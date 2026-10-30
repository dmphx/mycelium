package main

import (
	"bytes"
	"encoding/binary"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// These tests require the repository's full pinned Go toolchain/dependencies.
// They exercise the actual NFS filesystem adapter, without mounting an export.
func TestFilesystemUnavailableNeverOpensStub(t *testing.T) {
	oldRoot, oldFSH := stubRoot, fshRoot
	stubRoot, fshRoot = t.TempDir(), t.TempDir()
	t.Cleanup(func() { stubRoot, fshRoot = oldRoot, oldFSH })
	p := "movies/Fixture/Fixture.mkv"
	if err := os.MkdirAll(filepath.Dir(stubPath(p)), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(stubPath(p), bytes.Repeat([]byte{0}, 278), 0600); err != nil {
		t.Fatal(err)
	}
	tree := newTree()
	tree.fetchedAt = time.Now()
	tree.byPath[p] = entryInfo{token: "synthetic-unavailable", cached: false}
	fs := &sporeFS{tree: tree}
	if file, err := fs.Open(p); file != nil || !errors.Is(err, os.ErrPermission) {
		t.Fatalf("placeholder escaped: file=%v error=%v", file, err)
	}
	stat, err := fs.Stat(p)
	if err != nil || stat.Size() != 0 {
		t.Fatalf("placeholder advertised as playable size: %v %v", stat, err)
	}
	// A historical moov header is discovery metadata, not proof of a live movie.
	header := make([]byte, 34)
	binary.BigEndian.PutUint64(header[0:8], 1)
	binary.BigEndian.PutUint64(header[8:16], 1)
	binary.BigEndian.PutUint64(header[16:24], 2<<20)
	if err := os.WriteFile(fshPath("synthetic-expired"), header, 0600); err != nil {
		t.Fatal(err)
	}
	tree.byPath[p] = entryInfo{token: "synthetic-expired", cached: false, size: 2 << 20}
	if file, err := fs.Open(p); file != nil || !errors.Is(err, os.ErrPermission) {
		t.Fatalf("expired header substituted for availability: %v %v", file, err)
	}
	// Defense in depth: an old in-memory non-sidecar handle cannot read a stub.
	handle := &sporeFile{name: p, size: 278, stub: stubPath(p)}
	if n, err := handle.Read(make([]byte, 278)); n != 0 || !errors.Is(err, os.ErrPermission) {
		t.Fatalf("old placeholder handle read: %d %v", n, err)
	}
}

func TestFilesystemRealFileRecoveryAndSeek(t *testing.T) {
	media := bytes.Repeat([]byte{1, 2, 3, 4, 5, 6, 7, 8}, 1<<18)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.ServeContent(w, r, "fixture.mkv", unixZero, bytes.NewReader(media))
	}))
	defer server.Close()
	oldBase := myceliumBase
	myceliumBase = server.URL
	t.Cleanup(func() { myceliumBase = oldBase })
	tree := newTree()
	tree.fetchedAt = time.Now()
	p := "movies/Recovery/Recovery.mkv"
	tree.byPath[p] = entryInfo{token: "synthetic-real", size: int64(len(media)), cached: true}
	fs := &sporeFS{tree: tree}
	f, err := fs.Open(p)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	for _, offset := range []int64{0, 123456, int64(len(media)) - 100} {
		if _, err := f.Seek(offset, io.SeekStart); err != nil {
			t.Fatal(err)
		}
		buffer := make([]byte, 100)
		n, err := f.Read(buffer)
		if err != nil || n != 100 || !bytes.Equal(buffer, media[offset:offset+100]) {
			t.Fatalf("real bytes differ at%d: %d %v", offset, n, err)
		}
	}
}
