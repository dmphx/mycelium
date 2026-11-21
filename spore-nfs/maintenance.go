package main

// The control protocol fences Go metadata; it does not stop Python writers.
// A repair controller must independently earn that OS-level writer fence.

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"sort"
	"strings"
	"sync"
	"time"
)

var errMaintenance = errors.New("spore: metadata maintenance; media temporarily unavailable")
var maintenance = newMaintenance()

type maintenanceGate struct {
	mu           sync.Mutex
	transition   chan struct{}
	changed      chan struct{}
	mode         string
	owner        string
	instance     string
	active       map[string]int
	denied       uint64
	deniedByKind map[string]uint64
	monoEnd      time.Time
	hardEnd      time.Time
}

func newMaintenance() *maintenanceGate {
	var id [16]byte
	if _, err := rand.Read(id[:]); err != nil {
		panic(err)
	}
	m := &maintenanceGate{transition: make(chan struct{}, 1), changed: make(chan struct{}), mode: "normal", instance: hex.EncodeToString(id[:]), active: map[string]int{}, deniedByKind: map[string]uint64{}}
	m.transition <- struct{}{}
	return m
}

func (m *maintenanceGate) notify() { close(m.changed); m.changed = make(chan struct{}) }

// A permit covers the entire media read, refresh installation, or HTTP body.
// Closing admission happens before waiting for existing permits to retire.
func (m *maintenanceGate) begin(kind string, control bool) (func(), error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.mode != "normal" && !(control && m.mode == "resuming") {
		m.denied++
		m.deniedByKind[kind]++
		return nil, errMaintenance
	}
	m.active[kind]++
	var once sync.Once
	return func() { once.Do(func() { m.mu.Lock(); defer m.mu.Unlock(); m.active[kind]--; m.notify() }) }, nil
}

func (m *maintenanceGate) count() int {
	n := 0
	for _, v := range m.active {
		n += v
	}
	return n
}
func validOwner(s string) bool {
	if len(s) < 16 || len(s) > 96 {
		return false
	}
	for _, c := range s {
		if !(c >= 'a' && c <= 'z' || c >= '0' && c <= '9' || c == '-' || c == '_') {
			return false
		}
	}
	return true
}

func (m *maintenanceGate) enter(ctx context.Context, owner string, remaining time.Duration, hard time.Time, t *tree) error {
	select {
	case <-m.transition:
	case <-ctx.Done():
		return ctx.Err()
	}
	defer func() { m.transition <- struct{}{} }()
	if err := ctx.Err(); err != nil {
		return err
	}
	if !validOwner(owner) || remaining <= 0 || remaining > 45*time.Minute || !time.Now().Before(hard) {
		return errors.New("invalid maintenance admission")
	}
	m.mu.Lock()
	if m.mode != "normal" {
		m.mu.Unlock()
		return errors.New("maintenance already owned; no renewal")
	}
	m.mode = "draining"
	m.owner = owner
	m.monoEnd = time.Now().Add(remaining)
	m.hardEnd = hard
	m.notify()
	for m.count() != 0 {
		ch := m.changed
		m.mu.Unlock()
		select {
		case <-ch:
		case <-ctx.Done():
			return ctx.Err()
		}
		m.mu.Lock()
	}
	defer m.mu.Unlock()
	// On every refusal after admission, remain fenced for explicit recovery.
	if err := ctx.Err(); err != nil {
		return err
	}
	if !m.withinBudget() {
		return errors.New("maintenance admission expired")
	}
	t.mu.RLock()
	ready := len(t.byPath) > 0 && !t.fetchedAt.IsZero() && time.Since(t.fetchedAt) <= treeTTL
	t.mu.RUnlock()
	if !ready {
		return errors.New("current serving tree unready or too old")
	}
	m.mode = "active"
	m.notify()
	return nil
}

func (m *maintenanceGate) withinBudget() bool {
	now := time.Now()
	return now.Before(m.monoEnd) && now.Before(m.hardEnd)
}

type metadataSize struct {
	Path  string `json:"path"`
	Size  int64  `json:"size"`
	Mtime int64  `json:"mtime"`
}
type maintenanceSnapshot struct {
	Instance   string         `json:"instance"`
	Owner      string         `json:"owner"`
	Generation uint64         `json:"generation"`
	FetchedUTC time.Time      `json:"fetched_utc"`
	Entries    []treeEntry    `json:"entries"`
	Sizes      []metadataSize `json:"sizes"`
	SHA256     string         `json:"sha256"`
}

func (m *maintenanceGate) snapshot(owner string, paths []string, fs *sporeFS) (maintenanceSnapshot, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	var out maintenanceSnapshot
	if m.mode != "active" || m.owner != owner || !m.withinBudget() {
		return out, errors.New("not active owner or expired; still fenced")
	}
	if len(paths) > 256 {
		return out, errors.New("too many metadata paths")
	}
	fs.tree.mu.RLock()
	defer fs.tree.mu.RUnlock()
	out = maintenanceSnapshot{Instance: m.instance, Owner: owner, Generation: fs.tree.generation, FetchedUTC: fs.tree.fetchedAt}
	for path, e := range fs.tree.byPath {
		out.Entries = append(out.Entries, treeEntry{Token: e.token, Path: path, Size: e.size, Cached: e.cached})
	}
	sort.Slice(out.Entries, func(i, j int) bool { return out.Entries[i].Path < out.Entries[j].Path })
	seen := map[string]bool{}
	for _, p := range paths {
		if p == "" || strings.HasPrefix(p, "/") || fs.clean(p) != p || seen[p] {
			return maintenanceSnapshot{}, errors.New("invalid or repeated metadata path")
		}
		seen[p] = true
		e, ok := fs.tree.byPath[p]
		if !ok {
			return maintenanceSnapshot{}, errors.New("unknown metadata path")
		}
		size, mtime := fs.sizeAndMtime(p, e)
		out.Sizes = append(out.Sizes, metadataSize{Path: p, Size: size, Mtime: mtime.Unix()})
	}
	// Hash the exact sorted snapshot, not a separately reopened source.
	b, err := json.Marshal(out)
	if err != nil {
		return maintenanceSnapshot{}, err
	}
	h := sha256.Sum256(b)
	out.SHA256 = hex.EncodeToString(h[:])
	if !m.withinBudget() {
		return maintenanceSnapshot{}, errors.New("snapshot budget expired; still fenced")
	}
	return out, nil
}

type controlContextKey struct{}
type maintenanceTransport struct {
	base http.RoundTripper
	gate *maintenanceGate
}
type countedBody struct {
	io.ReadCloser
	done func()
}

func (b *countedBody) Read(p []byte) (int, error) {
	n, e := b.ReadCloser.Read(p)
	if e != nil {
		b.done()
	}
	return n, e
}
func (b *countedBody) Close() error { defer b.done(); return b.ReadCloser.Close() }
func (t *maintenanceTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	control, _ := r.Context().Value(controlContextKey{}).(bool)
	if control && (r.Method != http.MethodGet || r.URL.String() != myceliumBase+"/spore-nfs/tree") {
		return nil, errors.New("resume may only fetch the exact tree endpoint")
	}
	done, err := t.gate.begin("http", control)
	if err != nil {
		return nil, err
	}
	resp, err := t.base.RoundTrip(r)
	if err != nil {
		done()
		return nil, err
	}
	resp.Body = &countedBody{ReadCloser: resp.Body, done: done}
	return resp, nil
}

func (m *maintenanceGate) resume(ctx context.Context, owner string, t *tree) error {
	select {
	case <-m.transition:
	case <-ctx.Done():
		return ctx.Err()
	}
	defer func() { m.transition <- struct{}{} }()
	m.mu.Lock()
	if m.mode == "normal" || m.mode == "resuming" || owner != m.owner || m.count() != 0 {
		m.mu.Unlock()
		return errors.New("cannot resume unknown or active owner")
	}
	m.mode = "resuming"
	m.mu.Unlock()
	err := t.refreshWithContext(context.WithValue(ctx, controlContextKey{}, true), true)
	m.mu.Lock()
	defer m.mu.Unlock()
	if err != nil {
		m.mode = "draining"
		return fmt.Errorf("fresh tree failed; still fenced: %w", err)
	}
	t.mu.RLock()
	ready := len(t.byPath) > 0
	t.mu.RUnlock()
	if !ready {
		m.mode = "draining"
		return errors.New("fresh tree empty; still fenced")
	}
	m.mode = "normal"
	m.owner = ""
	m.notify()
	return nil
}

type maintenanceRequest struct {
	Command          string   `json:"command"`
	Owner            string   `json:"owner"`
	RemainingSeconds int      `json:"remaining_seconds"`
	HardEnd          string   `json:"hard_end"`
	Paths            []string `json:"paths"`
}

func parseMaintenanceRequest(raw []byte) (maintenanceRequest, error) {
	var req maintenanceRequest
	if len(raw) > 4096 {
		return req, errors.New("control request exceeds cap")
	}
	// encoding/json normally accepts duplicate keys. Authority must be singular.
	d := json.NewDecoder(bytes.NewReader(raw))
	first, err := d.Token()
	if err != nil || first != json.Delim('{') {
		return req, errors.New("control object required")
	}
	seen := map[string]bool{}
	for d.More() {
		key, err := d.Token()
		if err != nil {
			return req, err
		}
		s, ok := key.(string)
		if !ok || seen[s] {
			return req, errors.New("duplicate control key")
		}
		seen[s] = true
		var v json.RawMessage
		if err := d.Decode(&v); err != nil {
			return req, err
		}
	}
	if _, err := d.Token(); err != nil {
		return req, err
	}
	var extra any
	if err := d.Decode(&extra); err != io.EOF {
		return req, errors.New("exactly one control object required")
	}
	d = json.NewDecoder(bytes.NewReader(raw))
	d.DisallowUnknownFields()
	if err := d.Decode(&req); err != nil {
		return req, err
	}
	if req.Command == "status" {
		if req.Owner != "" || req.RemainingSeconds != 0 || req.HardEnd != "" || len(req.Paths) != 0 {
			return req, errors.New("status has no authority fields")
		}
	} else {
		if !validOwner(req.Owner) {
			return req, errors.New("invalid owner")
		}
		switch req.Command {
		case "enter":
			if req.RemainingSeconds < 1 || req.RemainingSeconds > 2700 || req.HardEnd == "" || len(req.Paths) != 0 {
				return req, errors.New("invalid enter fields")
			}
		case "snapshot":
			if req.RemainingSeconds != 0 || req.HardEnd != "" || len(req.Paths) > 256 {
				return req, errors.New("invalid snapshot fields")
			}
		case "resume":
			if req.RemainingSeconds != 0 || req.HardEnd != "" || len(req.Paths) != 0 {
				return req, errors.New("invalid resume fields")
			}
		default:
			return req, errors.New("unknown command")
		}
	}
	return req, nil
}

// Linux abstract sockets leave no stale filesystem inode to unlink on restart.
// Network-namespace isolation plus SO_PEERCRED UID0 replaces path permissions.
// Python runs as the unprivileged app user and cannot control this listener.
func serveMaintenance(listener *net.UnixListener, fs *sporeFS) error {
	slots := make(chan struct{}, 2)
	for {
		c, err := listener.AcceptUnix()
		if err != nil {
			return err
		}
		if !rootPeer(c) {
			c.Close()
			continue
		}
		select {
		case slots <- struct{}{}:
		default:
			c.Close()
			continue
		}
		go func() { defer func() { c.Close(); <-slots }(); handleMaintenance(c, fs) }()
	}
}

func handleMaintenance(c net.Conn, fs *sporeFS) {
	c.SetDeadline(time.Now().Add(40 * time.Second))
	raw, readErr := io.ReadAll(io.LimitReader(c, 4097))
	if len(raw) > 4096 {
		readErr = errors.New("control request exceeds cap")
	}
	var req maintenanceRequest
	err := readErr
	if err == nil {
		req, err = parseMaintenanceRequest(raw)
	}
	var result any
	if err == nil {
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		switch req.Command {
		case "enter":
			hard, e := time.Parse(time.RFC3339, req.HardEnd)
			if e != nil {
				err = e
			} else {
				err = maintenance.enter(ctx, req.Owner, time.Duration(req.RemainingSeconds)*time.Second, hard, fs.tree)
			}
		case "snapshot":
			result, err = maintenance.snapshot(req.Owner, req.Paths, fs)
		case "resume":
			err = maintenance.resume(ctx, req.Owner, fs.tree)
		case "status":
			maintenance.mu.Lock()
			denied := map[string]uint64{}
			for k, v := range maintenance.deniedByKind {
				denied[k] = v
			}
			result = map[string]any{"mode": maintenance.mode, "owner": maintenance.owner, "instance": maintenance.instance, "active": maintenance.count(), "denied": maintenance.denied, "denied_by_kind": denied}
			maintenance.mu.Unlock()
		default:
			err = errors.New("unknown control command")
		}
	}
	response := map[string]any{"ok": err == nil, "result": result}
	if err != nil {
		response["error"] = err.Error()
	}
	json.NewEncoder(c).Encode(response)
}
