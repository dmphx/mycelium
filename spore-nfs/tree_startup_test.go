package main

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
)

func TestInitialTreeWaitsForStatusThenExportsRealSize(t *testing.T) {
	var ready atomic.Bool
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.URL.Path != "/spore-nfs/tree" {
			t.Errorf("unexpected request: %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
			return
		}
		if !ready.Load() {
			w.Header().Set("Retry-After", "10")
			w.WriteHeader(http.StatusServiceUnavailable)
			return
		}
		fmt.Fprint(w, `{"entries":[{"token":"synthetic-startup","path":"movies/Fixture/Fixture.mkv","cached":true,"size":1326830205}]}`)
	}))
	defer server.Close()
	oldBase, oldRoot, oldFSH := myceliumBase, stubRoot, fshRoot
	myceliumBase, stubRoot, fshRoot = server.URL, t.TempDir(), t.TempDir()
	t.Cleanup(func() { myceliumBase, stubRoot, fshRoot = oldBase, oldRoot, oldFSH })
	tree := newTree()
	tree.refresh()
	if len(tree.byPath) != 0 || !tree.fetchedAt.IsZero() {
		t.Fatal("initializing status was cached as a successful tree")
	}
	ready.Store(true)
	// The failed first response cannot defer this retry by the five-minute TTL.
	tree.refreshIfStale()
	fs := &sporeFS{tree: tree}
	info, err := fs.Stat("movies/Fixture/Fixture.mkv")
	if err != nil || info.Size() != 1326830205 || !info.ModTime().Equal(mtimeCached) {
		t.Fatalf("first published tree did not expose real cached media: %v %v", info, err)
	}
	// Later transient failure retains the last coherent tree instead of erasing it.
	ready.Store(false)
	tree.refresh()
	if len(tree.byPath) != 1 || tree.byPath["movies/Fixture/Fixture.mkv"].size != 1326830205 {
		t.Fatal("later failure erased or downgraded cached media")
	}
	if calls.Load() != 3 {
		t.Fatalf("unexpected requests: %d", calls.Load())
	}
}
