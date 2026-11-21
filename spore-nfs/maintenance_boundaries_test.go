package main

import (
	"context"
	"encoding/binary"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

// All tests use local files or an in-memory RoundTripper, never a listener/provider.
func TestDeskExpiredOwnerCannotResume(t *testing.T) {
	m, fs := maintenanceFixture(t)
	enterFixture(t, m, fs)
	old := httpClient
	t.Cleanup(func() { httpClient = old })
	calls := 0
	httpClient = &http.Client{Transport: &maintenanceTransport{gate: m, base: roundTripFunc(func(*http.Request) (*http.Response, error) {
		calls++
		return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(`{"entries":[{"token":"fresh","path":"movies/Fresh.mkv","size":2097152,"cached":true}]}`))}, nil
	})}}
	m.mu.Lock()
	m.monoEnd = time.Now().Add(-time.Second)
	m.hardEnd = time.Now().Add(-time.Second)
	m.mu.Unlock()
	err := m.resume(context.Background(), fixtureOwner, fs.tree)
	m.mu.Lock()
	mode := m.mode
	m.mu.Unlock()
	if err == nil || mode == "normal" || calls != 0 {
		t.Fatalf("expired owner resumed: error=%v mode=%s fetches=%d generation=%d", err, mode, calls, fs.tree.generation)
	}
}

func TestDeskCaseAliasedAuthorityIsDuplicate(t *testing.T) {
	raw := `{"command":"enter","owner":"fixture-maintenance-operation","Owner":"different-root-operation","remaining_seconds":60,"hard_end":"2099-01-01T00:00:00Z"}`
	req, err := parseMaintenanceRequest([]byte(raw))
	if err == nil {
		t.Fatalf("ambiguous owner accepted; last writer selected %q", req.Owner)
	}
}

func TestDeskFrozenGenerationDoesNotChangeEffectiveSize(t *testing.T) {
	m, fs := maintenanceFixture(t)
	// A miss was cached before a header was installed, all before maintenance.
	fshMetaCache["fixture"] = fshMeta{fetched: time.Now().Add(-fshMissTTL + 100*time.Millisecond)}
	pre := make([]byte, 26)
	binary.BigEndian.PutUint64(pre[:8], 1)
	binary.BigEndian.PutUint64(pre[8:16], 1)
	binary.BigEndian.PutUint64(pre[16:24], 3<<20)
	if err := os.WriteFile(filepath.Join(fshRoot, "fixture.fsh"), pre, 0600); err != nil {
		t.Fatal(err)
	}
	enterFixture(t, m, fs)
	first, err := m.snapshot(fixtureOwner, []string{"series/Fixture/Fixture.mkv"}, fs)
	if err != nil {
		t.Fatal(err)
	}
	// No file/VI/tree writer runs during this interval; only the pre-existing miss expires.
	time.Sleep(180 * time.Millisecond)
	second, err := m.snapshot(fixtureOwner, []string{"series/Fixture/Fixture.mkv"}, fs)
	if err != nil {
		t.Fatal(err)
	}
	st, err := fs.Stat("series/Fixture/Fixture.mkv")
	if err != nil {
		t.Fatal(err)
	}
	if first.Generation != second.Generation || first.Sizes[0] != second.Sizes[0] || first.Sizes[0].Size != st.Size() {
		t.Fatalf("frozen generation changed metadata with unchanged files: gen %d/%d first=%+v second=%+v NFSsize=%d", first.Generation, second.Generation, first.Sizes[0], second.Sizes[0], st.Size())
	}
}

func TestMaintenanceResumeRejectsStaleOrMissingSourceProof(t *testing.T) {
	for _, header := range []string{"", "1", "invalid", "9223372036854775807"} {
		t.Run(header, func(t *testing.T) {
			m, fs := maintenanceFixture(t)
			enterFixture(t, m, fs)
			old := httpClient
			t.Cleanup(func() { httpClient = old })
			httpClient = &http.Client{Transport: &maintenanceTransport{gate: m, base: roundTripFunc(func(*http.Request) (*http.Response, error) {
				h := make(http.Header)
				h.Set("X-Spore-Tree-Built-Monotonic-Ns", header)
				return &http.Response{StatusCode: 200, Header: h, Body: io.NopCloser(strings.NewReader(`{"entries":[{"token":"fresh","path":"movies/Fresh.mkv","size":2097152,"cached":true}]}`))}, nil
			})}}
			err := m.resume(context.Background(), fixtureOwner, fs.tree)
			if err == nil || m.mode == "normal" || fs.tree.generation != 7 {
				t.Fatalf("unproven source admitted: %v %s %d", err, m.mode, fs.tree.generation)
			}
		})
	}
}
func TestMaintenanceResumeOriginalClocksIndividually(t *testing.T) {
	for _, clock := range []string{"monotonic", "wall"} {
		t.Run(clock, func(t *testing.T) {
			m, fs := maintenanceFixture(t)
			enterFixture(t, m, fs)
			old := httpClient
			t.Cleanup(func() { httpClient = old })
			httpClient = &http.Client{Transport: roundTripFunc(func(*http.Request) (*http.Response, error) { t.Fatal("expired resume fetched"); return nil, nil })}
			if clock == "monotonic" {
				m.monoEnd = time.Now().Add(-time.Second)
			} else {
				m.hardEnd = time.Now().Add(-time.Second)
			}
			if err := m.resume(context.Background(), fixtureOwner, fs.tree); err == nil || m.mode == "normal" {
				t.Fatal("expired clock reopened")
			}
		})
	}
}
func TestMaintenanceResumeExpirationDuringFetchStaysFenced(t *testing.T) {
	m, fs := maintenanceFixture(t)
	enterFixture(t, m, fs)
	old := httpClient
	t.Cleanup(func() { httpClient = old })
	httpClient = &http.Client{Transport: &maintenanceTransport{gate: m, base: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		<-r.Context().Done()
		return nil, r.Context().Err()
	})}}
	m.monoEnd = time.Now().Add(10 * time.Millisecond)
	if err := m.resume(context.Background(), fixtureOwner, fs.tree); err == nil || m.mode == "normal" || fs.tree.generation != 7 {
		t.Fatal("fetch expiry reopened or installed")
	}
}
func TestMaintenanceParserAllCaseAliasesRefused(t *testing.T) {
	for _, key := range []string{"Command", "OWNER", "Owner", "Paths", "Hard_End", "Remaining_Seconds"} {
		raw := `{"command":"status","` + key + `":null}`
		if _, err := parseMaintenanceRequest([]byte(raw)); err == nil {
			t.Fatalf("accepted alias %s", key)
		}
	}
}

func TestMaintenanceResumeLockWaitExpiryPreservesGeneration(t *testing.T) {
	m, fs := maintenanceFixture(t)
	enterFixture(t, m, fs)
	old := httpClient
	t.Cleanup(func() { httpClient = old })
	fetched := make(chan struct{})
	httpClient = &http.Client{Transport: &maintenanceTransport{gate: m, base: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		built, e := monotonicNanos()
		if e != nil {
			return nil, e
		}
		h := make(http.Header)
		h.Set("X-Spore-Tree-Built-Monotonic-Ns", strconv.FormatInt(built, 10))
		close(fetched)
		return &http.Response{StatusCode: 200, Header: h, Body: io.NopCloser(strings.NewReader(`{"entries":[{"token":"fresh","path":"movies/Fresh.mkv","size":2097152,"cached":true}]}`))}, nil
	})}}
	m.monoEnd = time.Now().Add(300 * time.Millisecond)
	fs.tree.mu.Lock()
	result := make(chan error, 1)
	go func() { result <- m.resume(context.Background(), fixtureOwner, fs.tree) }()
	select {
	case <-fetched:
	case <-time.After(2 * time.Second):
		fs.tree.mu.Unlock()
		t.Fatal("resume did not reach synthetic fetch")
	}
	time.Sleep(400 * time.Millisecond)
	fs.tree.mu.Unlock()
	err := <-result
	if err == nil || fs.tree.generation != 7 || m.mode == "normal" || len(fs.tree.frozenSizes) != 1 {
		t.Fatalf("expired lock wait changed state: %v gen%d mode%s", err, fs.tree.generation, m.mode)
	}
	if _, ok := fs.tree.byPath["series/Fixture/Fixture.mkv"]; !ok {
		t.Fatal("original serving path lost")
	}
}
