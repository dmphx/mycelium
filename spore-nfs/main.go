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
	treeTTL      = envDurSecOr("SPORE_TREE_TTL_SEC", 300*time.Second)
	// Until the first non-empty tree loads, refresh retries every startupRetryTTL
	// (not treeTTL): on a cold start spore-nfs can beat mycelium's gunicorn up, so
	// the initial refresh fails and an empty tree must be retried fast -- otherwise
	// Plex could see an empty library for a full treeTTL.
	startupRetryTTL = 10 * time.Second
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

func envOrInt(k string, def int) int {
	if v := os.Getenv(k); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n >= 0 {
			return n
		}
	}
	return def
}

// envDurSecOr reads an integer number of seconds from env var k and returns it
// as a Duration, or def when unset/invalid. Lets the tree-refresh cadence be
// tuned without a rebuild (SPORE_TREE_TTL_SEC).
func envDurSecOr(k string, def time.Duration) time.Duration {
	if v := os.Getenv(k); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			return time.Duration(n) * time.Second
		}
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

const maxRetries429 = 4
const retryBaseDelay = 300 * time.Millisecond

// errRateLimited signals that a Range GET exhausted its 429 retries. It is kept
// distinct from a generic failure so readRange can tell "the CDN is throttling
// this (still-valid) URL" apart from "this URL is stale": the former must NOT
// drop the cached URL and re-resolve via spore-stream (which 302s straight back
// to the same URL and doubles the request rate that is feeding the 429); the
// latter must.
var errRateLimited = errors.New("cdn range GET: 429 rate limited")

// rangeGetWithRetry issues a Range GET, retrying with backoff on a 429
// instead of failing immediately: a rate limit means the CDN needs a moment
// to clear, not that the request is doomed. Concurrent reads (multiple
// viewers, or a client's own read-ahead) can legitimately overlap on the
// same CDN link. Same retry policy as spore-smb's range_get_with_retry
// (Rust) and mp4_faststart.py's _get() (Python) -- this Go path hits the
// identical TorBox CDN and was the one consumer that hadn't been patched.
func rangeGetWithRetry(client *http.Client, url string, offset, length int64) (*http.Response, error) {
	for attempt := 0; ; attempt++ {
		req, err := http.NewRequest(http.MethodGet, url, nil)
		if err != nil {
			return nil, err
		}
		req.Header.Set("Range", fmt.Sprintf("bytes=%d-%d", offset, offset+length-1))
		resp, err := client.Do(req)
		if err != nil {
			return nil, err
		}
		if resp.StatusCode == http.StatusTooManyRequests && attempt < maxRetries429 {
			resp.Body.Close()
			backoff := retryBaseDelay * time.Duration(int64(1)<<uint(attempt))
			log.Printf("range GET %s: 429 rate limited, retrying in %s (attempt %d/%d)",
				url, backoff, attempt+1, maxRetries429)
			time.Sleep(backoff)
			continue
		}
		return resp, nil
	}
}

// fetchRange issues a Range GET (via rangeGetWithRetry) against a known URL
// (either the mycelium spore-stream endpoint or a directly-cached CDN url)
// and returns up to length bytes, or an error for anything other than
// 200/206.
func fetchRange(client *http.Client, url string, offset, length int64) ([]byte, error) {
	resp, err := rangeGetWithRetry(client, url, offset, length)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusTooManyRequests {
		// Retries exhausted and still throttled. Report it as such (not a
		// generic status error) so the caller keeps a still-valid URL instead
		// of re-resolving into the same throttled endpoint.
		return nil, errRateLimited
	}
	if resp.StatusCode != 206 && resp.StatusCode != 200 {
		return nil, fmt.Errorf("range GET %s: status %d", url, resp.StatusCode)
	}
	return io.ReadAll(io.LimitReader(resp.Body, length))
}

func readRange(token string, offset, length int64) ([]byte, error) {
	// Try a cached direct-CDN url first, if we have one. TorBox presigned
	// urls and catbox's own materialize cache both expire well before our
	// 50min TTL does in practice (idle cleanup, TorBox rotating the link),
	// so a cache hit here is not a guarantee the url still works -- treat
	// any failure as "stale", drop it, and fall through to re-resolving via
	// spore-stream instead of surfacing the error to the NFS caller.
	// Silent failures here previously showed up as ffmpeg's "I/O error"
	// with nothing at all logged on the mycelium side, since a cached-url
	// read bypasses mycelium entirely.
	cdnURLMu.RLock()
	cached, ok := cdnURLCache[token]
	cdnURLMu.RUnlock()
	if ok && time.Now().Before(cached.expires) {
		data, err := fetchRange(httpClient, cached.url, offset, length)
		if err == nil {
			return data, nil
		}
		if errors.Is(err, errRateLimited) {
			// The URL is valid, just rate limited. Dropping it and re-resolving
			// via spore-stream only 302s back to this same URL and issues a
			// second request into the same throttle -- exactly the amplification
			// that turns one throttled title into a 429 storm. Keep the URL and
			// surface the throttle; the NFS client re-issues the read shortly,
			// by which point the limit has usually eased.
			return nil, err
		}
		log.Printf("cached CDN url for %s failed (%v), re-resolving via spore-stream", token, err)
		cdnURLMu.Lock()
		delete(cdnURLCache, token)
		cdnURLMu.Unlock()
	}

	target := myceliumBase + "/spore-stream/" + token
	resp, err := rangeGetWithRetry(noRedirectClient, target, offset, length)
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
		return fetchRange(httpClient, loc, offset, length)
	}

	if resp.StatusCode != 206 && resp.StatusCode != 200 {
		return nil, fmt.Errorf("range GET %s: status %d", token, resp.StatusCode)
	}
	return io.ReadAll(io.LimitReader(resp.Body, length))
}

// coalesceRange collapses concurrent fetches of the same window into a single
// upstream readRange. Callers arriving while a fetch for the same (token,start)
// is in flight wait for it and share its bytes rather than issuing their own
// request. The blocking cache-miss read, the background prefetch, a second
// Watch-Together viewer, and a client's own extra connections all hitting the
// same 16MB window thus become one CDN Range GET instead of several -- which is
// what keeps TorBox's per-URL 429 rate limit from tripping. Keyed on the window
// start (not length): every consumer of a given window fetches the same
// grid-aligned span, and a follower that happens to want fewer bytes just slices
// less out of the shared buffer.
type inflightRange struct {
	done chan struct{}
	data []byte
	err  error
}

var (
	inflightMu sync.Mutex
	inflight   = map[string]*inflightRange{}
)

func coalesceRange(token string, start, length int64) ([]byte, error) {
	key := token + ":" + strconv.FormatInt(start, 10)

	inflightMu.Lock()
	if f, ok := inflight[key]; ok {
		inflightMu.Unlock()
		<-f.done
		return f.data, f.err
	}
	f := &inflightRange{done: make(chan struct{})}
	inflight[key] = f
	inflightMu.Unlock()

	f.data, f.err = readRange(token, start, length)

	inflightMu.Lock()
	delete(inflight, key)
	inflightMu.Unlock()
	close(f.done)
	return f.data, f.err
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
		// Sticky-real: a title that was already converted (has a built .fsh)
		// keeps serving as the real file even after its torrent idle-releases
		// from the account. Plex recorded it as a real Direct Play item, so if
		// spore-nfs suddenly served the 240-byte stub here Plex would read a
		// truncated file and playback would break. Instead serve the real size
		// (moov straight from the local .fsh, mdat re-materialized on demand via
		// bufferedRead -> /spore-stream), so the item stays playable across the
		// cache/expire cycle with no Plex re-analysis needed. Never-converted
		// titles (no .fsh) still serve the stub and transcode via the wrapper.
		if m := fshMetaFor(info.token); m.ok && m.cdnSize > 0 {
			return &sporeFile{name: p, token: info.token, size: m.cdnSize, cached: true}, nil
		}
		sp := stubPath(p)
		st, err := os.Stat(sp)
		if err != nil {
			return nil, err
		}
		return &sporeFile{name: p, token: info.token, size: st.Size(), cached: false, stub: sp}, nil
	}
	// The .fsh's cdn_size is the ACTUAL servable size (from the CDN HEAD when the
	// moov-first header was built). The tree's size can be a stale/nominal value
	// from a different release; if it over-reports, Plex reads past the real end,
	// gets 416s, and Direct Play stalls. Prefer the .fsh size whenever present.
	size := info.size
	if m := fshMetaFor(info.token); m.ok && m.cdnSize > 0 {
		size = m.cdnSize
	} else if size <= 0 {
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
		// Prefer the .fsh's real cdn_size over the tree's (possibly stale) size --
		// see Open(). Keeps Stat/ReadDir consistent with what is actually servable.
		if m := fshMetaFor(info.token); m.ok && m.cdnSize > 0 {
			return m.cdnSize, mtimeCached
		}
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
	// Sticky-real (see Open): a converted title keeps reporting its real size
	// and cached mtime once idle-released, so Plex keeps Direct-Playing it
	// rather than seeing a stub-sized file and breaking. Reporting the same
	// mtime band it had while cached means Plex never registers a flip, so no
	// re-analysis is triggered either.
	if m := fshMetaFor(info.token); m.ok && m.cdnSize > 0 {
		return m.cdnSize, mtimeCached
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
	readAheadSize      = 16 << 20  // 16MB per window
	readAheadWindows   = 3         // slots per token
	scanProbeThreshold = 256 << 10 // below this, treat as a metadata probe, not streaming
	probeMinFetch      = 1 << 20   // floor for probe-sized fetches (avoid 1:1 tiny requests)
)

// prefetchMaxInflight bounds how many read-ahead windows a single title may be
// fetching at once. All viewers of a title share one readAheadSet (and one
// s.pending map), so two Watch-Together viewers at different offsets would
// otherwise each launch their own prefetch on top of their foreground read;
// those extra concurrent Range GETs against one TorBox CDN URL are what push it
// over its 429 rate limit. 1 = at most one outstanding prefetch per title
// (foreground read + one prefetch => peak 2 concurrent per URL); 0 disables
// read-ahead entirely. Tunable via SPORE_PREFETCH_MAX_INFLIGHT.
var prefetchMaxInflight = envOrInt("SPORE_PREFETCH_MAX_INFLIGHT", 1)

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
	data, err := coalesceRange(token, start, fetchLen)

	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.pending, start)
	if err != nil {
		return
	}
	// Never cache a short prefetch (cold mdat streamed fewer bytes than asked
	// before the CDN was ready); a truncated window would later serve false
	// EOFs to a sequential reader that crosses into it.
	if int64(len(data)) < fetchLen && start+int64(len(data)) < fileSize {
		return
	}
	s.store(readAheadWindow{data: data, start: start, used: s.clock})
}

func bufferedRead(token string, offset, want, fileSize, mdatStart int64) ([]byte, error) {
	readAheadMu.Lock()
	s, ok := readAheads[token]
	if !ok {
		s = &readAheadSet{pending: map[int64]bool{}}
		readAheads[token] = s
	}
	readAheadMu.Unlock()

	gridS := gridStart(offset)
	start := gridS
	// bufferedRead only ever serves mdat: reads inside the moov header are
	// answered straight from the .fsh in Read(). A read-ahead window must
	// therefore never begin inside the moov. gridStart rounds an mdat offset in
	// the first 16MB down to 0, which made readRange ask spore-stream for a
	// range crossing the moov/mdat boundary. Cold, spore-stream sends the moov
	// instantly (from the .fsh) then stalls on the not-yet-materialized mdat;
	// the truncated response came back as a short buffer, got cached as a full
	// window, and every read past it then returned a false EOF -- Direct Play
	// died a couple MB in and "offered the next episode". Pinning the first
	// window to the mdat boundary keeps every fetch a clean pure-mdat range
	// that spore-stream serves whole.
	if mdatStart > 0 && start < mdatStart {
		start = mdatStart
	}

	s.mu.Lock()
	defer s.mu.Unlock()
	s.clock++

	w, ok := s.findWindow(start)
	if !ok {
		// Genuine cache miss (first read, or a seek) -- unavoidably blocks
		// the caller. Forcing a full 16MB fetch here regardless of `want`
		// made small one-off reads (e.g. Plex's library scanner probing a
		// file's header for duration/codec info, not streaming it) wait for
		// a full window before getting anything back -- observed on the NAS
		// as several titles getting a wrong, truncated duration recorded
		// because the scanner gave up before that fetch completed. Below
		// scanProbeThreshold, fetch close to what's actually needed (with a
		// small floor so it's not literally one HTTP request per byte);
		// real sequential playback naturally asks for much more than this
		// per read, so it still gets the full-window batching that fixed
		// the earlier per-chunk-latency stutter.
		fetchLen := int64(readAheadSize)
		if want < scanProbeThreshold {
			fetchLen = want
			if fetchLen < probeMinFetch {
				fetchLen = probeMinFetch
			}
		}
		if offset+want-start > fetchLen {
			fetchLen = offset + want - start
		}
		if start+fetchLen > fileSize {
			fetchLen = fileSize - start
		}
		data, err := coalesceRange(token, start, fetchLen)
		if err != nil {
			return nil, err
		}
		nw := readAheadWindow{data: data, start: start, used: s.clock}
		// Only cache a window that came back whole. A short read (cold mdat:
		// spore-stream ended the stream before the CDN was ready) must not be
		// stored as authoritative, or every later read in this grid cell would
		// keep hitting the truncated copy and returning a false EOF.
		if int64(len(data)) >= fetchLen || start+int64(len(data)) >= fileSize {
			s.store(nw)
		}
		w = &nw
	} else {
		w.used = s.clock
	}

	rel := offset - w.start
	dlen := int64(len(w.data))
	if rel < 0 {
		rel = 0
	}
	if rel >= dlen {
		// The window does not reach offset -- a short cold fetch. Returning an
		// empty slice here is a false EOF to the NFS client, which truncates
		// playback mid-file (the original "plays 1.5 min then offers the next
		// episode" bug). Fetch exactly what was asked for instead: by now the
		// mdat is usually materialized, so this returns real bytes and playback
		// continues. If it is genuinely past the end, signal EOF; otherwise
		// surface a retryable error rather than a silent truncation.
		if offset >= fileSize {
			return nil, io.EOF
		}
		direct, err := readRange(token, offset, want)
		if err != nil {
			return nil, err
		}
		if len(direct) == 0 {
			return nil, fmt.Errorf("spore: short read at %d/%d, mdat not ready", offset, fileSize)
		}
		return direct, nil
	}
	end := rel + want
	if end > dlen {
		end = dlen
	}

	// Past the midpoint of this grid cell: start fetching the next one now,
	// in the background, so it's ready before a sequential reader reaches
	// the edge instead of blocking on a fresh fetch at that point. `next` is
	// based on the unclamped grid so the first (mdat-boundary) window still
	// hands off to the aligned 16MB grid.
	if rel > dlen/2 {
		next := gridS + readAheadSize
		// Cap outstanding prefetches per title (shared across all its viewers)
		// so read-ahead can't fan concurrent CDN requests out past what the
		// per-URL 429 limit tolerates. Read-ahead is best effort: if we're
		// already at the cap, skip it -- the foreground read still fetches on
		// demand when it reaches that window.
		if next < fileSize && len(s.pending) < prefetchMaxInflight {
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
	cdnSize     int64 // real size of the full title, from the .fsh preamble
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
	var pre [24]byte
	if _, err := io.ReadFull(f, pre[:]); err != nil {
		return fshMeta{}
	}
	ftypSize := int64(binary.BigEndian.Uint64(pre[0:8]))
	moovSize := int64(binary.BigEndian.Uint64(pre[8:16]))
	cdnSize := int64(binary.BigEndian.Uint64(pre[16:24]))
	if ftypSize < 0 || moovSize <= 0 || cdnSize <= 0 {
		return fshMeta{} // sentinel (MKV / already-fast): no cached moov to serve
	}
	headerSize := ftypSize + moovSize
	headerStart := st.Size() - headerSize
	// Preamble is 24 (legacy) or 32 (current); anything else means the file is
	// truncated/malformed -- fall through to the CDN path rather than serve junk.
	if headerStart != 24 && headerStart != 32 {
		return fshMeta{}
	}
	return fshMeta{headerStart: headerStart, headerSize: headerSize, cdnSize: cdnSize, ok: true}
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
		m := fshMetaFor(f.token)
		if m.ok && f.pos < m.headerSize {
			buf, err = fshHeaderRead(f.token, m, f.pos, want)
		} else {
			// mdatStart marks where the moov header ends and mdat begins in the
			// virtual layout; bufferedRead uses it to keep read-ahead windows
			// from straddling that boundary (a straddling window short-reads
			// cold mdat and truncates playback -- see there). 0 for header-less
			// (MKV/sentinel) tokens, which map 1:1 to the CDN.
			var mdatStart int64
			if m.ok {
				mdatStart = m.headerSize
			}
			buf, err = bufferedRead(f.token, f.pos, want, f.size, mdatStart)
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
		for {
			// Steady-state cadence is treeTTL, but keep retrying every startupRetryTTL
			// until the tree is non-empty so a cold-start race with gunicorn recovers
			// fast instead of waiting a full treeTTL.
			t.mu.RLock()
			ready := len(t.byPath) > 0
			t.mu.RUnlock()
			if ready {
				time.Sleep(treeTTL)
			} else {
				time.Sleep(startupRetryTTL)
			}
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
