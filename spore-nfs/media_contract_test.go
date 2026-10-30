package main

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"
)

var unixZero = time.Unix(0, 0)

func TestPlayableMediaAvailability(t *testing.T) {
	for _, c := range []struct {
		name   string
		cached bool
		size   int64
		header int64
		ok     bool
	}{
		{"never-converted", false, 0, 0, false},
		{"legacy-236-byte-stub", true, 236, 0, false},
		{"guarded-278-byte-stub", true, 278, 0, false},
		{"unknown-size", true, 0, 0, false},
		{"negative-size", true, -1, 0, false},
		{"stale-moov-is-not-availability", false, 2 << 20, 2 << 20, false},
		{"cached-real-mkv", true, 2 << 20, 0, true},
		{"cached-real-mp4", true, 2 << 20, 2 << 20, true},
		{"mismatched-release-header", true, 2 << 20, 3 << 20, false},
	} {
		t.Run(c.name, func(t *testing.T) {
			n, err := playableMediaSize(c.cached, c.size, c.header)
			if (err == nil) != c.ok || (!c.ok && !errors.Is(err, os.ErrPermission)) {
				t.Fatalf("size=%d error=%v", n, err)
			}
			if c.ok && n != c.size {
				t.Fatalf("real size changed: %d", n)
			}
		})
	}
}

func TestMediaResponseRefusals(t *testing.T) {
	for _, c := range []struct {
		name, cr string
		status   int
		cl       int64
		offset   int64
		body     []byte
	}{
		{"legacy-stub-200", "", 200, 236, 0, make([]byte, 236)},
		{"guarded-stub-206", "bytes 0-277/278", 206, 278, 0, make([]byte, 278)},
		{"html-error", "", 503, 100, 0, make([]byte, 100)},
		{"auth-error", "", 403, 100, 0, make([]byte, 100)},
		{"missing-range", "", 206, 4096, 0, make([]byte, 4096)},
		{"malformed-range", "bytes 0-4095/*", 206, 4096, 0, make([]byte, 4096)},
		{"wrong-start", "bytes 0-4095/2097152", 206, 4096, 4096, make([]byte, 4096)},
		{"short-range", "bytes 0-277/2097152", 206, 278, 0, make([]byte, 278)},
		{"wrong-length", "bytes 0-4095/2097152", 206, 278, 0, make([]byte, 4096)},
		{"truncated-real-response", "bytes 0-4095/2097152", 206, 4096, 0, make([]byte, 278)},
		{"ignored-seek", "", 200, 2097152, 4096, make([]byte, 4096)},
		{"unknown-whole-size", "", 200, -1, 0, make([]byte, 4096)},
		{"overflow", "bytes 0-4095/9999999999999999999999", 206, 4096, 0, make([]byte, 4096)},
	} {
		t.Run(c.name, func(t *testing.T) {
			h := http.Header{}
			h.Set("Content-Range", c.cr)
			r := &http.Response{StatusCode: c.status, Header: h, ContentLength: c.cl, Body: io.NopCloser(bytes.NewReader(c.body))}
			data, err := readMediaResponse(r, c.offset, 4096)
			if err == nil || data != nil {
				t.Fatalf("unqualified bytes escaped: len=%d error=%v", len(data), err)
			}
		})
	}
}

func TestConsumerRangeRecoveryAndSeek(t *testing.T) {
	// A real HTTP consumer boundary using the same production response reader.
	// The server is an owned fixture, not Plex or a provider.
	media := bytes.Repeat([]byte{0x1a, 0x45, 0xdf, 0xa3, 0x33, 0x77, 0xaa, 0xff}, 1<<18)
	var ready atomic.Bool
	var requests atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		if !ready.Load() {
			// Reproduce a successful HTTP delivery of a guarded placeholder.
			w.Header().Set("Content-Length", "278")
			w.Write(make([]byte, 278))
			return
		}
		http.ServeContent(w, r, "fixture.mkv", unixZero, bytes.NewReader(media))
	}))
	defer server.Close()
	read := func(offset, length int64) ([]byte, error) {
		req, _ := http.NewRequest(http.MethodGet, server.URL, nil)
		req.Header.Set("Range", fmt.Sprintf("bytes=%d-%d", offset, offset+length-1))
		r, err := server.Client().Do(req)
		if err != nil {
			return nil, err
		}
		defer r.Body.Close()
		return readMediaResponse(r, offset, length)
	}
	if b, err := read(0, 4096); err == nil || b != nil {
		t.Fatal("placeholder delivered as media")
	}
	ready.Store(true)
	for _, offset := range []int64{0, 4096, 123456, int64(len(media)) - 100} {
		got, err := read(offset, 4096)
		end := offset + 4096
		if end > int64(len(media)) {
			end = int64(len(media))
		}
		if err != nil || !bytes.Equal(got, media[offset:end]) {
			t.Fatalf("recovery/seek offset%d: len=%d error=%v", offset, len(got), err)
		}
	}
	if requests.Load() != 5 {
		t.Fatalf("unexpected retries or background requests: %d", requests.Load())
	}
}

func TestRealSyntheticMovieRangeReassembly(t *testing.T) {
	p := os.Getenv("SPORE_SYNTHETIC_MOVIE")
	if p == "" {
		t.Skip("explicit generated synthetic consumer movie not supplied")
	}
	media, err := os.ReadFile(p)
	if err != nil || int64(len(media)) < minimumMediaSize {
		t.Fatalf("qualified generated media unavailable: %v", err)
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.ServeContent(w, r, "synthetic.mkv", unixZero, bytes.NewReader(media))
	}))
	defer server.Close()
	var assembled []byte
	for offset := int64(0); offset < int64(len(media)); offset += 65536 {
		req, _ := http.NewRequest(http.MethodGet, server.URL, nil)
		req.Header.Set("Range", fmt.Sprintf("bytes=%d-%d", offset, offset+65535))
		r, err := server.Client().Do(req)
		if err != nil {
			t.Fatal(err)
		}
		part, err := readMediaResponse(r, offset, 65536)
		r.Body.Close()
		if err != nil {
			t.Fatal(err)
		}
		assembled = append(assembled, part...)
	}
	if !bytes.Equal(assembled, media) {
		t.Fatal("consumer byte reassembly differs from actual synthetic movie")
	}
	out := os.Getenv("SPORE_REASSEMBLED_MOVIE")
	if out == "" || filepath.Clean(out) == filepath.Clean(p) {
		t.Fatal("distinct owned consumer output path required")
	}
	if err := os.WriteFile(out, assembled, 0600); err != nil {
		t.Fatal(err)
	}
}
