package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

const fixtureOwner = "fixture-maintenance-operation"

func maintenanceFixture(t *testing.T) (*maintenanceGate, *sporeFS) {
	t.Helper()
	old := maintenance
	oldRoot := fshRoot
	oldCache := fshMetaCache
	maintenance = newMaintenance()
	fshRoot = t.TempDir()
	fshMetaCache = map[string]fshMeta{}
	t.Cleanup(func() { maintenance = old; fshRoot = oldRoot; fshMetaCache = oldCache })
	tr := newTree()
	tr.byPath["series/Fixture/Fixture.mkv"] = entryInfo{token: "fixture", size: 2 << 20, cached: true}
	tr.fetchedAt = time.Now()
	tr.generation = 7
	return maintenance, &sporeFS{tree: tr}
}
func enterFixture(t *testing.T, m *maintenanceGate, fs *sporeFS) {
	t.Helper()
	ctx, c := context.WithTimeout(context.Background(), time.Second)
	defer c()
	if err := m.enter(ctx, fixtureOwner, time.Minute, time.Now().Add(time.Minute), fs.tree); err != nil {
		t.Fatal(err)
	}
}
func TestMaintenanceKeepsMetadataPastTreeTTLAndRejectsMedia(t *testing.T) {
	m, fs := maintenanceFixture(t)
	enterFixture(t, m, fs)
	// Simulate crossing the 300-second refresh cadence after admission.
	fs.tree.fetchedAt = time.Now().Add(-301 * time.Second)
	old := httpClient
	httpClient = &http.Client{Transport: roundTripFunc(func(*http.Request) (*http.Response, error) {
		t.Fatal("maintenance attempted HTTP")
		return nil, errors.New("blocked")
	})}
	t.Cleanup(func() { httpClient = old })
	s, err := m.snapshot(fixtureOwner, []string{"series/Fixture/Fixture.mkv"}, fs)
	if err != nil {
		t.Fatal(err)
	}
	info, err := fs.Stat("series/Fixture/Fixture.mkv")
	if err != nil || info.Size() != s.Sizes[0].Size || s.Generation != 7 || s.Sizes[0].Size != 2<<20 {
		t.Fatalf("metadata mismatch: %+v %v", s, err)
	}
	f, err := fs.Open("series/Fixture/Fixture.mkv")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.Read(make([]byte, 1)); !errors.Is(err, errMaintenance) {
		t.Fatalf("media admitted: %v", err)
	}
	if fs.tree.generation != 7 {
		t.Fatal("generation replaced")
	}
}
func TestMaintenanceDrainsPermitsAndPreventsNewWork(t *testing.T) {
	m, fs := maintenanceFixture(t)
	done, _ := m.begin("media", false)
	ctx, c := context.WithTimeout(context.Background(), time.Second)
	defer c()
	result := make(chan error, 1)
	go func() { result <- m.enter(ctx, fixtureOwner, time.Minute, time.Now().Add(time.Minute), fs.tree) }()
	deadline := time.Now().Add(time.Second)
	for {
		m.mu.Lock()
		mode := m.mode
		m.mu.Unlock()
		if mode == "draining" {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("not draining")
		}
		time.Sleep(time.Millisecond)
	}
	if _, err := m.begin("http", false); !errors.Is(err, errMaintenance) {
		t.Fatal("new network admitted")
	}
	select {
	case <-result:
		t.Fatal("entered with live read")
	default:
	}
	done()
	done()
	if err := <-result; err != nil {
		t.Fatal(err)
	}
}
func TestMaintenanceDrainTimeoutAndExpiredOwnerStayFenced(t *testing.T) {
	m, fs := maintenanceFixture(t)
	done, _ := m.begin("http", false)
	ctx, c := context.WithTimeout(context.Background(), time.Millisecond)
	defer c()
	if err := m.enter(ctx, fixtureOwner, time.Minute, time.Now().Add(time.Minute), fs.tree); err == nil {
		t.Fatal("drain ignored")
	}
	done()
	if _, err := m.begin("media", false); err == nil {
		t.Fatal("timeout reopened media")
	}
	if _, err := m.snapshot(fixtureOwner, nil, fs); err == nil {
		t.Fatal("unearned snapshot")
	}
	m.mu.Lock()
	m.mode = "active"
	m.monoEnd = time.Now().Add(-time.Second)
	m.mu.Unlock()
	if _, err := m.snapshot(fixtureOwner, nil, fs); err == nil {
		t.Fatal("expired snapshot")
	}
	if _, err := m.begin("media", false); err == nil {
		t.Fatal("expiry reopened media")
	}
	if err := m.enter(context.Background(), fixtureOwner, time.Minute, time.Now().Add(time.Minute), fs.tree); err == nil {
		t.Fatal("renewal allowed")
	}
}
func TestMaintenanceFreshnessAndOwnershipRefusals(t *testing.T) {
	for _, kind := range []string{"empty", "old", "wrong-owner", "duplicate-path", "unknown-path", "invalid-path", "clock"} {
		t.Run(kind, func(t *testing.T) {
			m, fs := maintenanceFixture(t)
			if kind == "old" {
				fs.tree.fetchedAt = time.Now().Add(-treeTTL - time.Second)
			}
			if kind == "empty" {
				fs.tree.byPath = map[string]entryInfo{}
			}
			err := m.enter(context.Background(), fixtureOwner, time.Minute, time.Now().Add(time.Minute), fs.tree)
			if kind == "old" || kind == "empty" {
				if err == nil {
					t.Fatal("unready tree admitted")
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			owner := fixtureOwner
			paths := []string{"series/Fixture/Fixture.mkv"}
			switch kind {
			case "wrong-owner":
				owner = "wrong-owner-operation"
			case "duplicate-path":
				paths = append(paths, paths[0])
			case "unknown-path":
				paths = []string{"missing.mkv"}
			case "invalid-path":
				paths = []string{"series/../Fixture.mkv"}
			case "clock":
				m.hardEnd = time.Now().Add(-time.Second)
			}
			if _, err = m.snapshot(owner, paths, fs); err == nil {
				t.Fatal("unsafe snapshot accepted")
			}
		})
	}
}
func TestMaintenanceFshMismatchUsesActualStatPredicate(t *testing.T) {
	m, fs := maintenanceFixture(t)
	pre := make([]byte, 26)
	binary.BigEndian.PutUint64(pre[:8], 1)
	binary.BigEndian.PutUint64(pre[8:16], 1)
	binary.BigEndian.PutUint64(pre[16:24], 3<<20)
	if err := os.WriteFile(filepath.Join(fshRoot, "fixture.fsh"), pre, 0600); err != nil {
		t.Fatal(err)
	}
	enterFixture(t, m, fs)
	s, err := m.snapshot(fixtureOwner, []string{"series/Fixture/Fixture.mkv"}, fs)
	if err != nil {
		t.Fatal(err)
	}
	st, err := fs.Stat("series/Fixture/Fixture.mkv")
	if err != nil {
		t.Fatal(err)
	}
	if s.Sizes[0].Size != 0 || st.Size() != 0 || s.Sizes[0].Mtime != mtimeStub.Unix() {
		t.Fatal("historical .fsh bypassed availability")
	}
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }
func TestMaintenanceHTTPPermitCoversBodyAndNoNetworkAfterEntry(t *testing.T) {
	m, fs := maintenanceFixture(t)
	var calls atomic.Int32
	rt := &maintenanceTransport{gate: m, base: roundTripFunc(func(*http.Request) (*http.Response, error) {
		calls.Add(1)
		return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader("body"))}, nil
	})}
	req, _ := http.NewRequest("GET", "http://fixture.invalid/", nil)
	resp, err := rt.RoundTrip(req)
	if err != nil {
		t.Fatal(err)
	}
	m.mu.Lock()
	n := m.count()
	m.mu.Unlock()
	if n != 1 {
		t.Fatal("body not counted")
	}
	resp.Body.Close()
	resp.Body.Close()
	enterFixture(t, m, fs)
	if _, err := rt.RoundTrip(req); !errors.Is(err, errMaintenance) || calls.Load() != 1 {
		t.Fatal("maintenance reached network")
	}
}
func TestMaintenanceResumeRequiresFreshNormalTree(t *testing.T) {
	m, fs := maintenanceFixture(t)
	enterFixture(t, m, fs)
	oldClient, oldBase := httpClient, myceliumBase
	t.Cleanup(func() { httpClient = oldClient; myceliumBase = oldBase })
	var mode atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/spore-nfs/tree" {
			t.Error("unexpected endpoint")
		}
		built, err := monotonicNanos()
		if err != nil {
			t.Error(err)
			return
		}
		w.Header().Set("X-Spore-Tree-Built-Monotonic-Ns", strconv.FormatInt(built, 10))
		if mode.Load() == 0 {
			http.Error(w, "not ready", 503)
			return
		}
		if mode.Load() == 1 {
			io.WriteString(w, `{"entries":[]}`)
			return
		}
		io.WriteString(w, `{"entries":[{"token":"fresh","path":"movies/Fresh.mkv","size":2097152,"cached":true}]}`)
	}))
	defer srv.Close()
	myceliumBase = srv.URL
	httpClient = &http.Client{Timeout: time.Second, Transport: &maintenanceTransport{base: http.DefaultTransport, gate: m}}
	if err := m.resume(context.Background(), "wrong-owner-operation", fs.tree); err == nil {
		t.Fatal("wrong owner resumed")
	}
	if err := m.resume(context.Background(), fixtureOwner, fs.tree); err == nil {
		t.Fatal("failed tree resumed")
	}
	if _, err := m.begin("media", false); err == nil {
		t.Fatal("failed resume unblocked")
	}
	mode.Store(1)
	if err := m.resume(context.Background(), fixtureOwner, fs.tree); err == nil {
		t.Fatal("empty tree resumed")
	}
	if fs.tree.generation != 7 || len(fs.tree.byPath) != 1 {
		t.Fatal("failed resume replaced frozen generation")
	}
	mode.Store(2)
	if err := m.resume(context.Background(), fixtureOwner, fs.tree); err != nil {
		t.Fatal(err)
	}
	done, err := m.begin("media", false)
	if err != nil {
		t.Fatal(err)
	}
	done()
	if fs.tree.generation != 8 {
		t.Fatalf("fresh generation not installed: %d", fs.tree.generation)
	}
}
func TestMaintenanceParserRefusesAmbiguousAuthority(t *testing.T) {
	good := `{"command":"enter","owner":"fixture-maintenance-operation","remaining_seconds":60,"hard_end":"2099-01-01T00:00:00Z"}`
	for _, s := range []string{`null`, `[]`, good + `{}`, strings.Replace(good, `"command":"enter"`, `"command":"enter","command":"resume"`, 1), strings.Replace(good, `60`, `9223372036854775807`, 1), strings.Replace(good, `60`, `-1`, 1), strings.Replace(good, `60`, `2701`, 1), strings.Replace(good, `"enter"`, `"status"`, 1), strings.Replace(good, `"owner"`, `"unknown"`, 1), strings.Repeat(" ", 4097), `{"command":"resume","owner":"short"}`} {
		if _, err := parseMaintenanceRequest([]byte(s)); err == nil {
			t.Fatalf("accepted %q", s)
		}
	}
	for _, s := range []string{good, `{"command":"status"}`, `{"command":"snapshot","owner":"fixture-maintenance-operation","paths":["series/Fixture.mkv"]}`, `{"command":"resume","owner":"fixture-maintenance-operation"}`} {
		if _, err := parseMaintenanceRequest([]byte(s)); err != nil {
			t.Fatal(err)
		}
	}
}

// A finite input stream models the required client half-close; no SSH or Docker.
type fixtureConn struct {
	r *bytes.Reader
	w bytes.Buffer
}

func (c *fixtureConn) Read(p []byte) (int, error)       { return c.r.Read(p) }
func (c *fixtureConn) Write(p []byte) (int, error)      { return c.w.Write(p) }
func (c *fixtureConn) Close() error                     { return nil }
func (c *fixtureConn) LocalAddr() net.Addr              { return nil }
func (c *fixtureConn) RemoteAddr() net.Addr             { return nil }
func (c *fixtureConn) SetDeadline(time.Time) error      { return nil }
func (c *fixtureConn) SetReadDeadline(time.Time) error  { return nil }
func (c *fixtureConn) SetWriteDeadline(time.Time) error { return nil }
func TestMaintenanceMalformedActualHandlerHasNoEffects(t *testing.T) {
	m, fs := maintenanceFixture(t)
	for _, s := range []string{`{"command":"enter","owner":"fixture-maintenance-operation","remaining_seconds":-1}`, `{"command":"enter","owner":"fixture-maintenance-operation","remaining_seconds":60,"hard_end":"invalid"}`, `{"command":"status","extra":1}`} {
		c := &fixtureConn{r: bytes.NewReader([]byte(s))}
		handleMaintenance(c, fs)
		if !strings.Contains(c.w.String(), `"ok":false`) || m.mode != "normal" {
			t.Fatalf("malformed request changed state %s", c.w.String())
		}
	}
}
func TestMaintenanceControlRedirectRefusedBeforeNetwork(t *testing.T) {
	m, fs := maintenanceFixture(t)
	enterFixture(t, m, fs)
	m.mode = "resuming"
	rt := &maintenanceTransport{gate: m, base: roundTripFunc(func(*http.Request) (*http.Response, error) { t.Fatal("redirect reached network"); return nil, nil })}
	req, _ := http.NewRequestWithContext(context.WithValue(context.Background(), controlContextKey{}, true), "GET", "http://fixture.invalid/other", nil)
	if _, err := rt.RoundTrip(req); err == nil {
		t.Fatal("control redirect accepted")
	}
}

func TestMaintenanceDrainsRefreshBeforeFreezingGeneration(t *testing.T) {
	m, fs := maintenanceFixture(t)
	oldClient, oldBase := httpClient, myceliumBase
	t.Cleanup(func() { httpClient = oldClient; myceliumBase = oldBase })
	started, release := make(chan struct{}), make(chan struct{})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		close(started)
		<-release
		io.WriteString(w, `{"entries":[{"token":"new","path":"movies/New.mkv","size":2097152,"cached":true}]}`)
	}))
	defer srv.Close()
	myceliumBase = srv.URL
	httpClient = &http.Client{Timeout: time.Second, Transport: &maintenanceTransport{base: http.DefaultTransport, gate: m}}
	refreshed := make(chan struct{})
	go func() { fs.tree.refresh(); close(refreshed) }()
	<-started
	result := make(chan error, 1)
	ctx, c := context.WithTimeout(context.Background(), time.Second)
	defer c()
	go func() { result <- m.enter(ctx, fixtureOwner, time.Minute, time.Now().Add(time.Minute), fs.tree) }()
	for deadline := time.Now().Add(time.Second); ; {
		m.mu.Lock()
		mode := m.mode
		m.mu.Unlock()
		if mode == "draining" {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("not draining")
		}
		time.Sleep(time.Millisecond)
	}
	select {
	case err := <-result:
		t.Fatalf("entered before refresh retirement: %v", err)
	default:
	}
	close(release)
	<-refreshed
	if err := <-result; err != nil {
		t.Fatal(err)
	}
	s, err := m.snapshot(fixtureOwner, []string{"movies/New.mkv"}, fs)
	if err != nil || s.Generation != 8 || len(s.Entries) != 1 {
		t.Fatalf("snapshot precedes drained refresh: %+v %v", s, err)
	}
	fs.tree.refresh()
	if fs.tree.generation != 8 {
		t.Fatal("late refresh replaced frozen tree")
	}
}
func TestMaintenanceControllerLossDoesNotUnfreeze(t *testing.T) {
	m, fs := maintenanceFixture(t)
	enterFixture(t, m, fs)
	// No control connection has to stay open to retain the fence.
	if _, err := m.begin("http", false); err == nil {
		t.Fatal("connection loss reopened HTTP")
	}
	if _, err := m.begin("media", false); err == nil {
		t.Fatal("connection loss reopened media")
	}
	s, err := m.snapshot(fixtureOwner, nil, fs)
	if err != nil || s.Owner != fixtureOwner {
		t.Fatal("fence custody lost")
	}
}

func TestMaintenanceBothRuntimeHTTPClientsUseTheGate(t *testing.T) {
	for _, client := range []*http.Client{httpClient, noRedirectClient} {
		tr, ok := client.Transport.(*maintenanceTransport)
		if !ok || tr.gate != maintenance || tr.base == nil {
			t.Fatal("an actual runtime HTTP client bypasses the maintenance gate")
		}
	}
}
func TestMaintenanceSnapshotHashAndInstanceAreBound(t *testing.T) {
	m, fs := maintenanceFixture(t)
	enterFixture(t, m, fs)
	s, err := m.snapshot(fixtureOwner, []string{"series/Fixture/Fixture.mkv"}, fs)
	if err != nil {
		t.Fatal(err)
	}
	first := s.SHA256
	s.SHA256 = ""
	b, err := json.Marshal(s)
	if err != nil {
		t.Fatal(err)
	}
	h := sha256.Sum256(b)
	if first != hex.EncodeToString(h[:]) || s.Instance == "" || s.Instance == newMaintenance().instance {
		t.Fatal("snapshot hash/instance not bound")
	}
}

func TestMaintenanceTransitionWaitCannotRenewBudget(t *testing.T) {
	m, fs := maintenanceFixture(t)
	<-m.transition
	ctx, c := context.WithTimeout(context.Background(), time.Second)
	defer c()
	result := make(chan error, 1)
	go func() {
		result <- m.enter(ctx, fixtureOwner, 20*time.Millisecond, time.Now().Add(time.Minute), fs.tree)
	}()
	time.Sleep(45 * time.Millisecond)
	m.transition <- struct{}{}
	if err := <-result; err == nil {
		t.Fatal("transition wait renewed the original duration")
	}
	if _, err := m.begin("media", false); err == nil {
		t.Fatal("expired admission automatically resumed")
	}
}
