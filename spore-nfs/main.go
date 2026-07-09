// spore-nfs exposes Mycelium's virtual library as a read-only NFSv3 export.
//
// It carries no media data itself: every Stat() asks Mycelium's existing
// /spore-stream/<token> endpoint (via HEAD) for the real file size, and
// every Read() re-issues that same request with a Range header. Mycelium
// already knows how to serve moov-first cached headers and Range-proxy the
// rest from TorBox (mp4_faststart.py, catbox.materialize()) -- this server
// is only a protocol adapter from NFS reads to those existing HTTP calls.
//
// Because the file Plex reads here has real bytes and a real size, Direct
// Play is the correct outcome instead of the black-screen problem the fake
// stub .mkv approach hits on some clients.
package main

import (
	"crypto/sha256"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"math"
	"net"
	"net/http"
	"os"
	"path"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/go-git/go-billy/v5"
	nfs "github.com/willscott/go-nfs"
	nfshelper "github.com/willscott/go-nfs/helpers"
)

var (
	myceliumBase = envOr("MYCELIUM_BASE", "http://mycelium:8088")
	listenAddr   = envOr("LISTEN_ADDR", ":2049")
	stubRoot     = envOr("SPORE_STUB_ROOT", "/data/plex-media")
	fshRoot      = envOr("SPORE_FSH_ROOT", stubRoot+"/.fsh")
	treeTTL      = 10 * time.Second
	httpClient   = &http.Client{Timeout: 30 * time.Second}
)

// Distinct mtime bands per representation so Plex notices a stub<->real flip
// (a title becoming cached, or a torrent expiring) and re-analyzes it.
var (
	mtimeCached = time.Unix(2_000_000_000, 0)
	mtimeStub   = time.Unix(1_000_000_000, 0)
)

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

// ---- virtual tree -----------------------------------------------------

type treeEntry struct {
	Token  string `json:"token"`
	Path   string `json:"path"`   // e.g. "movies/Civil War (2024)/Civil War (2024).mkv"
	Size   int64  `json:"size"`   // real cached size in bytes; 0 when uncached/unknown
	Cached bool   `json:"cached"` // true => serve the real file; false => serve the stub
}

// entryInfo is the per-path record the tree keeps in memory.
type entryInfo struct {
	token  string
	size   int64
	cached bool
}

type tree struct {
	mu        sync.RWMutex
	byPath    map[string]entryInfo // path -> {token,size,cached}
	dirs      map[string]bool      // every ancestor directory of every file
	childMap  map[string][]string  // dir -> immediate child names (files + subdirs)
	fetchedAt time.Time
}

func newTree() *tree {
	return &tree{
		byPath:   map[string]entryInfo{},
		dirs:     map[string]bool{"": true},
		childMap: map[string][]string{},
	}
}

// parentOf returns the parent directory of p, with the export root expressed as "".
func parentOf(p string) string {
	d := path.Dir(p)
	if d == "." || d == "/" {
		return ""
	}
	return d
}

func (t *tree) refreshIfStale() {
	t.mu.RLock()
	stale := time.Since(t.fetchedAt) > treeTTL
	t.mu.RUnlock()
	if !stale {
		return
	}
	t.refresh()
}

func (t *tree) refresh() {
	resp, err := httpClient.Get(myceliumBase + "/spore-nfs/tree")
	if err != nil {
		log.Printf("tree refresh: %v", err)
		return
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		log.Printf("tree refresh: unexpected status %d", resp.StatusCode)
		return
	}
	var out struct {
		Entries []treeEntry `json:"entries"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		log.Printf("tree refresh decode: %v", err)
		return
	}

	byPath := map[string]entryInfo{}
	dirs := map[string]bool{"": true}
	childMap := map[string][]string{}
	childSeen := map[string]map[string]bool{}
	addChild := func(parent, name string) {
		if name == "" {
			return
		}
		s := childSeen[parent]
		if s == nil {
			s = map[string]bool{}
			childSeen[parent] = s
		}
		if s[name] {
			return
		}
		s[name] = true
		childMap[parent] = append(childMap[parent], name)
	}
	for _, e := range out.Entries {
		clean := strings.Trim(path.Clean("/"+e.Path), "/")
		byPath[clean] = entryInfo{token: e.Token, size: e.Size, cached: e.Cached}
		addChild(parentOf(clean), path.Base(clean))
		for dir := path.Dir(clean); dir != "." && dir != "/"; dir = path.Dir(dir) {
			if !dirs[dir] {
				dirs[dir] = true
				addChild(parentOf(dir), path.Base(dir))
			}
			if dir == "." {
				break
			}
		}
	}

	t.mu.Lock()
	t.byPath = byPath
	t.dirs = dirs
	t.childMap = childMap
	t.fetchedAt = time.Now()
	t.mu.Unlock()
	log.Printf("tree refreshed: %d files, %d dirs", len(byPath), len(dirs))
}

func (t *tree) tokenFor(p string) (string, bool) {
	info, ok := t.infoFor(p)
	return info.token, ok
}

func (t *tree) infoFor(p string) (entryInfo, bool) {
	t.refreshIfStale()
	t.mu.RLock()
	defer t.mu.RUnlock()
	info, ok := t.byPath[p]
	return info, ok
}

func (t *tree) isDir(p string) bool {
	t.refreshIfStale()
	t.mu.RLock()
	defer t.mu.RUnlock()
	return t.dirs[p]
}

// children returns the immediate child names (files and subdirs) of dir. O(1)
// via the precomputed childMap -- a Plex library scan issues one READDIR per
// directory, so the old O(files) scan per call made cold scans crawl and time out.
func (t *tree) children(dir string) []string {
	t.refreshIfStale()
	t.mu.RLock()
	defer t.mu.RUnlock()
	return t.childMap[dir]
}

// ---- HTTP-backed file size / content -----------------------------------

// go-nfs's onRead() calls both fs.Open() and fs.Stat() on every single NFS
// READ RPC (NFSv3 is stateless -- see nfs_onread.go). Without this cache,
// every read chunk would trigger its own materializing HEAD to
// /spore-stream/<token>, throttling playback to whatever that round trip
// costs per chunk regardless of NFS rsize.
var (
	realSizeCacheMu sync.RWMutex
	realSizeCache   = map[string]struct {
		size    int64
		expires time.Time
	}{}
)

func peekRealSize(token string) (int64, bool) {
	realSizeCacheMu.RLock()
	defer realSizeCacheMu.RUnlock()
	e, ok := realSizeCache[token]
	if !ok || time.Now().After(e.expires) {
		return 0, false
	}
	return e.size, true
}

func cachedRealSize(token string) (int64, error) {
	if size, ok := peekRealSize(token); ok {
		return size, nil
	}
	size, err := realSize(token)
	if err != nil {
		return 0, err
	}
	realSizeCacheMu.Lock()
	realSizeCache[token] = struct {
		size    int64
		expires time.Time
	}{size: size, expires: time.Now().Add(30 * time.Minute)}
	realSizeCacheMu.Unlock()
	return size, nil
}

func realSize(token string) (int64, error) {
	req, err := http.NewRequest(http.MethodHead, myceliumBase+"/spore-stream/"+token, nil)
	if err != nil {
		return 0, err
	}
	resp, err := httpClient.Do(req)
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 && resp.StatusCode != 302 {
		return 0, fmt.Errorf("HEAD %s: status %d", token, resp.StatusCode)
	}
	cl := resp.Header.Get("Content-Length")
	if cl == "" {
		return 0, fmt.Errorf("HEAD %s: no Content-Length", token)
	}
	return strconv.ParseInt(cl, 10, 64)
}

// cheapSize asks Mycelium's TorBox checkcached-backed lookup for a file's
// size WITHOUT materializing it (no torrent add, no CDN URL fetch). Used
// for library scans (Attr/Stat/ReadDir), where realSize()'s materializing
// HEAD would otherwise add every single scanned item to TorBox just to
// learn its size.
func cheapSize(token string) (int64, error) {
	resp, err := httpClient.Get(myceliumBase + "/spore-nfs/size/" + token)
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return 0, fmt.Errorf("size lookup %s: status %d", token, resp.StatusCode)
	}
	var out struct {
		Size int64 `json:"size"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return 0, err
	}
	return out.Size, nil
}

// MKV items are served by spore-stream as a 302 to the real TorBox CDN URL
// once warm (offsets map 1:1, no moov rewriting needed for MKV). Chasing
// that redirect on every single NFS read adds a full extra network hop per
// chunk, which at 1MB-ish NFS read sizes adds up to real stutter on a
// 20+Mbps stream. Cache the resolved CDN URL per token and read directly
// from it afterwards -- only ever populated from an *observed* redirect, so
// it's never used for content spore-stream serves itself (e.g. the MP4
// virtual-moov layout, where byte offsets do NOT map to the raw CDN file).
var (
	cdnURLMu    sync.RWMutex
	cdnURLCache = map[string]struct {
		url     string
		expires time.Time
	}{}
)

var noRedirectClient = &http.Client{
	Timeout: 30 * time.Second,
	CheckRedirect: func(req *http.Request, via []*http.Request) error {
		return http.ErrUseLastResponse
	},
}

func readRange(token string, offset, length int64) ([]byte, error) {
	target := myceliumBase + "/spore-stream/" + token
	cdnURLMu.RLock()
	cached, ok := cdnURLCache[token]
	cdnURLMu.RUnlock()
	if ok && time.Now().Before(cached.expires) {
		target = cached.url
	}

	req, err := http.NewRequest(http.MethodGet, target, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Range", fmt.Sprintf("bytes=%d-%d", offset, offset+length-1))
	resp, err := noRedirectClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusFound || resp.StatusCode == http.StatusMovedPermanently {
		loc := resp.Header.Get("Location")
		if loc == "" {
			return nil, fmt.Errorf("range GET %s: redirect with no Location", token)
		}
		cdnURLMu.Lock()
		cdnURLCache[token] = struct {
			url     string
			expires time.Time
		}{url: loc, expires: time.Now().Add(50 * time.Minute)}
		cdnURLMu.Unlock()

		req2, err := http.NewRequest(http.MethodGet, loc, nil)
		if err != nil {
			return nil, err
		}
		req2.Header.Set("Range", fmt.Sprintf("bytes=%d-%d", offset, offset+length-1))
		resp2, err := httpClient.Do(req2)
		if err != nil {
			return nil, err
		}
		defer resp2.Body.Close()
		if resp2.StatusCode != 206 && resp2.StatusCode != 200 {
			return nil, fmt.Errorf("range GET (redirected) %s: status %d", token, resp2.StatusCode)
		}
		return io.ReadAll(io.LimitReader(resp2.Body, length))
	}

	if resp.StatusCode != 206 && resp.StatusCode != 200 {
		return nil, fmt.Errorf("range GET %s: status %d", token, resp.StatusCode)
	}
	return io.ReadAll(io.LimitReader(resp.Body, length))
}

// ---- billy.Filesystem implementation -----------------------------------

type sporeFS struct {
	tree *tree
}

func (fs *sporeFS) clean(p string) string {
	return strings.Trim(path.Clean("/"+filepathToSlash(p)), "/")
}

func filepathToSlash(p string) string { return strings.ReplaceAll(p, "\\", "/") }

func (fs *sporeFS) Root() string { return "/" }

func (fs *sporeFS) Create(filename string) (billy.File, error)      { return nil, billy.ErrReadOnly }
func (fs *sporeFS) OpenFile(filename string, flag int, perm os.FileMode) (billy.File, error) {
	return fs.Open(filename)
}
func (fs *sporeFS) Rename(oldpath, newpath string) error { return billy.ErrReadOnly }
func (fs *sporeFS) Remove(filename string) error         { return billy.ErrReadOnly }
func (fs *sporeFS) Join(elem ...string) string           { return path.Join(elem...) }
func (fs *sporeFS) TempFile(dir, prefix string) (billy.File, error) { return nil, billy.ErrReadOnly }
func (fs *sporeFS) MkdirAll(filename string, perm os.FileMode) error { return nil }
func (fs *sporeFS) Symlink(target, link string) error    { return billy.ErrReadOnly }
func (fs *sporeFS) Readlink(link string) (string, error) { return "", errors.New("not a symlink") }
func (fs *sporeFS) Chroot(path string) (billy.Filesystem, error) { return fs, nil }

func (fs *sporeFS) Open(filename string) (billy.File, error) {
	p := fs.clean(filename)
	if strings.HasSuffix(p, ".minfo") {
		// A .minfo sidecar is served straight from the on-disk stub tree so the
		// Plex transcoder wrapper can read the token when a stub (uncached) title
		// is played over NFS. Not in the media tree; opened by path.
		sp := stubPath(p)
		st, err := os.Stat(sp)
		if err != nil {
			return nil, err
		}
		return &sporeFile{name: p, size: st.Size(), cached: false, stub: sp}, nil
	}
	info, ok := fs.tree.infoFor(p)
	if !ok {
		return nil, os.ErrNotExist
	}
	if !info.cached {
		// Uncached: serve the on-disk stub (forces a Plex transcode, lazy-adds
		// to TorBox only on a real /spore-stream play). No CDN, no torrent add
		// during scans.
		sp := stubPath(p)
		st, err := os.Stat(sp)
		if err != nil {
			return nil, err
		}
		return &sporeFile{name: p, token: info.token, size: st.Size(), cached: false, stub: sp}, nil
	}
	size := info.size
	if size <= 0 {
		s, err := cachedRealSize(info.token)
		if err != nil {
			return nil, err
		}
		size = s
	}
	return &sporeFile{name: p, token: info.token, size: size, cached: true}, nil
}

func (fs *sporeFS) Stat(filename string) (os.FileInfo, error) {
	p := fs.clean(filename)
	if p == "" || fs.tree.isDir(p) {
		return dirInfo{name: path.Base(p)}, nil
	}
	if strings.HasSuffix(p, ".minfo") {
		// .minfo sidecar: report the on-disk stub sidecar so the wrapper's
		// `[ -f X.minfo ]` test and read succeed over NFS.
		st, err := os.Stat(stubPath(p))
		if err != nil {
			return nil, err
		}
		return fileInfo{name: path.Base(p), size: st.Size(), mtime: mtimeStub}, nil
	}
	info, ok := fs.tree.infoFor(p)
	if !ok {
		return nil, os.ErrNotExist
	}
	size, mtime := fs.sizeAndMtime(p, info)
	return fileInfo{name: path.Base(p), size: size, mtime: mtime}, nil
}
func (fs *sporeFS) Lstat(filename string) (os.FileInfo, error) { return fs.Stat(filename) }

// sizeAndMtime resolves the size and mtime to report for a path. Cached items
// report their real size (served via spore-stream); uncached items report the
// on-disk stub's size (served straight from SPORE_STUB_ROOT: no CDN, no add).
func (fs *sporeFS) sizeAndMtime(p string, info entryInfo) (int64, time.Time) {
	if info.cached {
		size := info.size
		// go-nfs calls Stat() on every READ RPC; if this token is already open
		// for real playback, reuse that size instead of another lookup.
		if s, ok := peekRealSize(info.token); ok {
			size = s
		}
		if size <= 0 {
			if s, err := cheapSize(info.token); err == nil {
				size = s
			}
		}
		return size, mtimeCached
	}
	if st, err := os.Stat(stubPath(p)); err == nil {
		return st.Size(), mtimeStub
	}
	return 0, mtimeStub
}

func stubPath(p string) string { return filepath.Join(stubRoot, filepath.FromSlash(p)) }

// stubRead serves a byte range from the tiny on-disk stub file.
func stubRead(pathStr string, offset, length int64) ([]byte, error) {
	fh, err := os.Open(pathStr)
	if err != nil {
		return nil, err
	}
	defer fh.Close()
	buf := make([]byte, length)
	n, err := fh.ReadAt(buf, offset)
	if err != nil && err != io.EOF {
		return nil, err
	}
	return buf[:n], nil
}

func (fs *sporeFS) ReadDir(dirname string) ([]os.FileInfo, error) {
	p := fs.clean(dirname)
	var out []os.FileInfo
	for _, name := range fs.tree.children(p) {
		child := path.Join(p, name)
		if fs.tree.isDir(child) {
			out = append(out, dirInfo{name: name})
			continue
		}
		info, ok := fs.tree.infoFor(child)
		if !ok {
			continue
		}
		size, mtime := fs.sizeAndMtime(child, info)
		out = append(out, fileInfo{name: name, size: size, mtime: mtime})
	}
	return out, nil
}

// ---- os.FileInfo implementations ---------------------------------------

type fileInfo struct {
	name  string
	size  int64
	mtime time.Time
}

func (f fileInfo) Name() string      { return f.name }
func (f fileInfo) Size() int64       { return f.size }
func (f fileInfo) Mode() os.FileMode { return 0444 }
func (f fileInfo) ModTime() time.Time {
	if f.mtime.IsZero() {
		return time.Unix(0, 0)
	}
	return f.mtime
}
func (f fileInfo) IsDir() bool        { return false }
func (f fileInfo) Sys() interface{}   { return nil }

type dirInfo struct{ name string }

func (d dirInfo) Name() string       { return d.name }
func (d dirInfo) Size() int64        { return 0 }
func (d dirInfo) Mode() os.FileMode  { return os.ModeDir | 0555 }
func (d dirInfo) ModTime() time.Time { return time.Unix(0, 0) }
func (d dirInfo) IsDir() bool        { return true }
func (d dirInfo) Sys() interface{}   { return nil }

// ---- billy.File: reads proxy to spore-stream via Range ------------------

// go-nfs re-Opens the file on every single READ RPC (see nfs_onread.go), so
// per-file-handle state doesn't survive between reads -- the read-ahead
// buffer has to live per-token instead, shared across those short-lived
// sporeFile instances.
const (
	readAheadSize    = 16 << 20 // 16MB per window
	readAheadWindows = 3        // slots per token
)

// A single window worked for one sequential reader, but real sessions have
// more than one: Plex's background analysis/thumbnail pass can read the
// same file concurrently at a different offset than the main playback
// stream. With only one slot, each reader kept evicting the other's window
// -- observed on the NAS as offsets ping-ponging between two regions,
// re-fetching 16MB on nearly every read instead of reusing it. A small set
// of windows (LRU-evicted) lets a few concurrent readers each keep their
// own recent window without stepping on each other.
type readAheadWindow struct {
	data  []byte
	start int64
	used  int64 // logical clock for LRU eviction
}

type readAheadSet struct {
	mu      sync.Mutex
	windows [readAheadWindows]readAheadWindow
	clock   int64
	pending map[int64]bool // window start offsets currently being prefetched
}

var (
	readAheadMu sync.Mutex
	readAheads  = map[string]*readAheadSet{}
)

// gridStart rounds offset down to a fixed readAheadSize boundary. Windows
// used to start wherever a cache miss happened to land, which meant two
// readers a few hundred KB apart opened two different, barely-overlapping
// 16MB windows instead of sharing one -- observed on the NAS as offsets
// crawling forward by ~1MB every ~5s, a fresh 16MB fetch every single time,
// nowhere near real-time bitrate. A fixed grid means every reader near the
// same position converges on the exact same window.
func gridStart(offset int64) int64 {
	return (offset / readAheadSize) * readAheadSize
}

func (s *readAheadSet) findWindow(start int64) (*readAheadWindow, bool) {
	for i := range s.windows {
		if s.windows[i].data != nil && s.windows[i].start == start {
			return &s.windows[i], true
		}
	}
	return nil, false
}

func (s *readAheadSet) store(w readAheadWindow) {
	lru := 0
	for i := range s.windows {
		if s.windows[i].used < s.windows[lru].used {
			lru = i
		}
	}
	s.windows[lru] = w
}

// prefetch fetches the grid-aligned window at start in the background and
// installs it once it arrives, so a reader that later crosses into it
// doesn't have to wait on the fetch itself.
func (s *readAheadSet) prefetch(token string, start, fileSize int64) {
	fetchLen := int64(readAheadSize)
	if start+fetchLen > fileSize {
		fetchLen = fileSize - start
	}
	data, err := readRange(token, start, fetchLen)

	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.pending, start)
	if err != nil {
		return
	}
	s.store(readAheadWindow{data: data, start: start, used: s.clock})
}

func bufferedRead(token string, offset, want, fileSize int64) ([]byte, error) {
	readAheadMu.Lock()
	s, ok := readAheads[token]
	if !ok {
		s = &readAheadSet{pending: map[int64]bool{}}
		readAheads[token] = s
	}
	readAheadMu.Unlock()

	start := gridStart(offset)

	s.mu.Lock()
	defer s.mu.Unlock()
	s.clock++

	w, ok := s.findWindow(start)
	if !ok {
		// Genuine cache miss (first read, or a seek) -- unavoidably blocks
		// the caller. want can exceed one grid cell if the requester asks
		// for more than readAheadSize in one go; fetchWindow always fetches
		// at least a full cell from `start`, so extend it here if needed.
		fetchLen := int64(readAheadSize)
		if offset+want-start > fetchLen {
			fetchLen = offset + want - start
		}
		if start+fetchLen > fileSize {
			fetchLen = fileSize - start
		}
		data, err := readRange(token, start, fetchLen)
		if err != nil {
			return nil, err
		}
		nw := readAheadWindow{data: data, start: start, used: s.clock}
		s.store(nw)
		w = &nw
	} else {
		w.used = s.clock
	}

	rel := offset - w.start
	dlen := int64(len(w.data))
	// A short window (the CDN returned fewer bytes than the item's reported
	// size -- e.g. the materialized release differs from what checkcached sized)
	// can put rel past the data. Clamp both ends so the slice is always valid: a
	// read past the available bytes yields nothing instead of panicking and
	// crashing the whole NFS server for every client.
	if rel < 0 {
		rel = 0
	}
	if rel > dlen {
		rel = dlen
	}
	end := rel + want
	if end > dlen {
		end = dlen
	}

	// Past the midpoint of this grid cell: start fetching the next one now,
	// in the background, so it's ready before a sequential reader reaches
	// the edge instead of blocking on a fresh fetch at that point.
	if rel > int64(len(w.data))/2 {
		next := start + readAheadSize
		if next < fileSize {
			if _, have := s.findWindow(next); !have && !s.pending[next] {
				s.pending[next] = true
				go s.prefetch(token, next, fileSize)
			}
		}
	}
	return w.data[rel:end], nil
}

// ---- fast-start moov header served straight from the .fsh cache -----------
//
// mp4_faststart.py builds a moov-first header ([ftyp][rewritten moov]) for
// cached MP4 titles and stores it as <token>.fsh. That file lives on the same
// filesystem as this server (spore-nfs runs inside the mycelium container), so
// reads that fall inside the header can be served straight from it -- no HTTP,
// no CDN, and crucially no catbox.materialize(). Plex's over-NFS analyze pass
// only ever reads the moov; routing it through /spore-stream made every moov
// read pay materialize()'s ~30s cold torrent-add, which exceeds the NFS read
// timeout and stalled Direct Play analysis. mdat reads (real playback) still
// go through bufferedRead -> /spore-stream and materialize on demand.
//
// .fsh layout: [preamble][ftyp+rewritten_moov]. Preamble is 32 bytes
// (ftyp_size, moov_size, cdn_size, moov_offset as big-endian uint64) in the
// current format, 24 in a legacy one. Either way the header is the trailing
// (ftyp_size+moov_size) bytes, so headerStart = fileSize - headerSize is
// format-agnostic. moov_size == 0 is a sentinel (MKV / already-fast) with no
// cached moov; such tokens report "no header" and fall through to bufferedRead.
type fshMeta struct {
	headerStart int64
	headerSize  int64
	ok          bool
	fetched     time.Time
}

var (
	fshMetaMu    sync.RWMutex
	fshMetaCache = map[string]fshMeta{}
)

const fshMissTTL = 2 * time.Minute

func fshPath(token string) string { return filepath.Join(fshRoot, token+".fsh") }

// fshMetaFor locates the cached moov header for a token. Hits are cached
// forever (a .fsh is immutable once atomically written); misses are re-checked
// after fshMissTTL because a title's .fsh may be built later (on first real
// playback), after which its moov reads should go fast too.
func fshMetaFor(token string) fshMeta {
	fshMetaMu.RLock()
	m, ok := fshMetaCache[token]
	fshMetaMu.RUnlock()
	if ok && (m.ok || time.Since(m.fetched) < fshMissTTL) {
		return m
	}
	m = readFshMeta(token)
	m.fetched = time.Now()
	fshMetaMu.Lock()
	fshMetaCache[token] = m
	fshMetaMu.Unlock()
	return m
}

func readFshMeta(token string) fshMeta {
	fp := fshPath(token)
	st, err := os.Stat(fp)
	if err != nil {
		return fshMeta{}
	}
	f, err := os.Open(fp)
	if err != nil {
		return fshMeta{}
	}
	defer f.Close()
	var pre [16]byte
	if _, err := io.ReadFull(f, pre[:]); err != nil {
		return fshMeta{}
	}
	ftypSize := int64(binary.BigEndian.Uint64(pre[0:8]))
	moovSize := int64(binary.BigEndian.Uint64(pre[8:16]))
	if ftypSize < 0 || moovSize <= 0 {
		return fshMeta{} // sentinel (MKV / already-fast): no cached moov to serve
	}
	headerSize := ftypSize + moovSize
	headerStart := st.Size() - headerSize
	// Preamble is 24 (legacy) or 32 (current); anything else means the file is
	// truncated/malformed -- fall through to the CDN path rather than serve junk.
	if headerStart != 24 && headerStart != 32 {
		return fshMeta{}
	}
	return fshMeta{headerStart: headerStart, headerSize: headerSize, ok: true}
}

// fshHeaderRead serves [offset, offset+want) of the virtual moov-first file from
// the cached header bytes in the .fsh. The caller guarantees offset < headerSize;
// the slice is clamped to the header boundary, so a read spanning header+mdat
// yields only the header part (a short read) and the next read continues into
// mdat via bufferedRead.
func fshHeaderRead(token string, m fshMeta, offset, want int64) ([]byte, error) {
	end := offset + want
	if end > m.headerSize {
		end = m.headerSize
	}
	n := end - offset
	if n <= 0 {
		return nil, nil
	}
	f, err := os.Open(fshPath(token))
	if err != nil {
		return nil, err
	}
	defer f.Close()
	buf := make([]byte, n)
	got, err := f.ReadAt(buf, m.headerStart+offset)
	if err != nil && err != io.EOF {
		return nil, err
	}
	return buf[:got], nil
}

type sporeFile struct {
	name   string
	token  string
	size   int64
	pos    int64
	cached bool
	stub   string // on-disk stub path, used when !cached
	mu     sync.Mutex
}

func (f *sporeFile) Name() string { return f.name }

func (f *sporeFile) Read(p []byte) (int, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.pos >= f.size {
		return 0, io.EOF
	}
	want := int64(len(p))
	if f.pos+want > f.size {
		want = f.size - f.pos
	}
	var buf []byte
	var err error
	if f.cached {
		// A read inside the moov header is served straight from the local .fsh
		// cache (no materialize) so Plex's over-NFS analyze pass runs fast; mdat
		// reads (real playback) fall through to the materialize + CDN path.
		if m := fshMetaFor(f.token); m.ok && f.pos < m.headerSize {
			buf, err = fshHeaderRead(f.token, m, f.pos, want)
		} else {
			buf, err = bufferedRead(f.token, f.pos, want, f.size)
		}
	} else {
		buf, err = stubRead(f.stub, f.pos, want)
	}
	if err != nil {
		return 0, err
	}
	n := copy(p, buf)
	f.pos += int64(n)
	return n, nil
}

func (f *sporeFile) ReadAt(p []byte, off int64) (int, error) {
	f.mu.Lock()
	f.pos = off
	f.mu.Unlock()
	return f.Read(p)
}

func (f *sporeFile) Seek(offset int64, whence int) (int64, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	switch whence {
	case io.SeekStart:
		f.pos = offset
	case io.SeekCurrent:
		f.pos += offset
	case io.SeekEnd:
		f.pos = f.size + offset
	}
	return f.pos, nil
}

func (f *sporeFile) Write(p []byte) (int, error)   { return 0, billy.ErrReadOnly }
func (f *sporeFile) Close() error                  { return nil }
func (f *sporeFile) Lock() error                   { return nil }
func (f *sporeFile) Unlock() error                 { return nil }
func (f *sporeFile) Truncate(size int64) error     { return billy.ErrReadOnly }

// ---- stable file handles ------------------------------------------------

// The default CachingHandler mints a random UUID per path in a 4096-entry LRU.
// That breaks this deployment two ways: the map is empty after a spore-nfs
// restart (every handle Plex still holds goes STALE, so the library vanishes),
// and 4096 handles is far too few for a 100k-file library (handles evict mid
// scan). stableHandler instead derives each handle deterministically from the
// path (sha256), so the same path always maps to the same handle -- handles
// survive restarts and never evict. A path->handle map, pre-seeded from the
// tree, lets FromHandle resolve any handle the client cached before a restart.
type stableHandler struct {
	nfs.Handler // embedded NullAuthHandler supplies Mount/Change/FSStat
	fs          billy.Filesystem
	mu          sync.RWMutex
	toPath      map[string][]string // handle bytes (as string) -> path components
	lastLen     int
}

func newStableHandler(base nfs.Handler, fs billy.Filesystem) *stableHandler {
	return &stableHandler{Handler: base, fs: fs, toPath: map[string][]string{}}
}

func pathHandle(parts []string) []byte {
	sum := sha256.Sum256([]byte(path.Join(parts...)))
	return sum[:]
}

func (h *stableHandler) ToHandle(_ billy.Filesystem, p []string) []byte {
	fh := pathHandle(p)
	k := string(fh)
	h.mu.RLock()
	_, known := h.toPath[k]
	h.mu.RUnlock()
	if !known {
		cp := append([]string(nil), p...)
		h.mu.Lock()
		h.toPath[k] = cp
		h.mu.Unlock()
	}
	return fh
}

func (h *stableHandler) FromHandle(fh []byte) (billy.Filesystem, []string, error) {
	h.mu.RLock()
	parts, ok := h.toPath[string(fh)]
	h.mu.RUnlock()
	if !ok {
		return nil, nil, &nfs.NFSStatusError{NFSStatus: nfs.NFSStatusStale}
	}
	return h.fs, parts, nil
}

func (h *stableHandler) InvalidateHandle(billy.Filesystem, []byte) error { return nil }

func (h *stableHandler) HandleLimit() int { return math.MaxInt32 }

// syncFromTree pre-registers a deterministic handle for every path in the tree so
// a handle the client cached before a restart still resolves. Additive (never
// removes), so handles learned on access (e.g. .minfo sidecars) are kept. Skips
// work when the path count is unchanged.
func (h *stableHandler) syncFromTree(t *tree) {
	t.mu.RLock()
	total := len(t.byPath) + len(t.dirs)
	if total == h.lastLen {
		t.mu.RUnlock()
		return
	}
	paths := make([][]string, 0, total+1)
	paths = append(paths, []string{}) // export root
	for p := range t.byPath {
		paths = append(paths, strings.Split(p, "/"))
	}
	for d := range t.dirs {
		if d != "" {
			paths = append(paths, strings.Split(d, "/"))
		}
	}
	t.mu.RUnlock()

	h.mu.Lock()
	for _, parts := range paths {
		k := string(pathHandle(parts))
		if _, ok := h.toPath[k]; !ok {
			h.toPath[k] = parts
		}
	}
	h.lastLen = total
	h.mu.Unlock()
}

// ---- main ---------------------------------------------------------------

func main() {
	t := newTree()
	t.refresh()

	fs := &sporeFS{tree: t}
	base := nfshelper.NewNullAuthHandler(fs)
	handler := newStableHandler(base, fs)
	handler.syncFromTree(t)

	// Retry independently of incoming NFS requests: on a fresh start this
	// container can win the race against mycelium's own startup, and
	// refreshIfStale() alone won't retry again until something actually
	// asks the filesystem for a file, which never happens on a client
	// that gave up mounting after an empty first listing. Re-seed handles
	// after every refresh so newly added paths are resolvable.
	go func() {
		ticker := time.NewTicker(treeTTL)
		defer ticker.Stop()
		for range ticker.C {
			t.refresh()
			handler.syncFromTree(t)
		}
	}()

	listener, err := net.Listen("tcp", listenAddr)
	if err != nil {
		log.Fatal(err)
	}
	log.Printf("spore-nfs listening on %s, backing store = %s", listenAddr, myceliumBase)
	if err := nfs.Serve(listener, handler); err != nil {
		log.Fatal(err)
	}
}
