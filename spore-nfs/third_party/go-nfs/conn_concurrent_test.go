package nfs

import (
	"context"
	"encoding/binary"
	"fmt"
	"io"
	"net"
	"sync/atomic"
	"testing"
	"time"
)

var testProgram uint32 = 0x40000000

func callFrame(xid, program, proc uint32, body []byte) []byte {
	words := []uint32{xid, 0, 2, program, 1, proc, 0, 0, 0, 0}
	frame := make([]byte, 4+4*len(words)+len(body))
	binary.BigEndian.PutUint32(frame, uint32(len(frame)-4)|(1<<31))
	for i, v := range words {
		binary.BigEndian.PutUint32(frame[4+i*4:], v)
	}
	copy(frame[44:], body)
	return frame
}

func readFrame(t *testing.T, c net.Conn) (uint32, []byte) {
	t.Helper()
	c.SetReadDeadline(time.Now().Add(2 * time.Second))
	var h [4]byte
	if _, err := io.ReadFull(c, h[:]); err != nil {
		t.Fatalf("response header: %v", err)
	}
	n := binary.BigEndian.Uint32(h[:])
	if n&(1<<31) == 0 || n&^(uint32(1)<<31) > 1<<20 {
		t.Fatalf("bad response frame %x", n)
	}
	b := make([]byte, n&^(uint32(1)<<31))
	if _, err := io.ReadFull(c, b); err != nil {
		t.Fatalf("response body: %v", err)
	}
	if len(b) < 24 {
		t.Fatalf("short RPC reply: %d", len(b))
	}
	return binary.BigEndian.Uint32(b), b[24:]
}

func transport(t *testing.T, handlers map[uint32]HandleFunc) (net.Conn, uint32, chan struct{}, context.CancelFunc) {
	t.Helper()
	program := atomic.AddUint32(&testProgram, 1)
	for proc, handler := range handlers {
		if err := RegisterMessageHandler(program, proc, handler); err != nil {
			t.Fatal(err)
		}
	}
	server, client := net.Pipe()
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	c := (&Server{}).newConn(server)
	go func() { defer close(done); c.serve(ctx) }()
	t.Cleanup(func() {
		cancel()
		client.Close()
		select {
		case <-done:
		case <-time.After(3 * time.Second):
			t.Error("transport did not retire")
		}
	})
	return client, program, done, cancel
}

func TestMetadataReplyOvertakesBlockedReadOnOneConnection(t *testing.T) {
	entered := make(chan struct{})
	release := make(chan struct{})
	defer close(release)
	handler := func(ctx context.Context, w *response, h Handler) error {
		if w.req.Header.Proc == 1 {
			close(entered)
			select {
			case <-release:
			case <-ctx.Done():
				return ctx.Err()
			}
		}
		b, err := io.ReadAll(w.req.Body)
		if err != nil {
			return err
		}
		return w.Write(b)
	}
	client, program, _, _ := transport(t, map[uint32]HandleFunc{1: handler, 2: handler})
	if _, err := client.Write(callFrame(10, program, 1, []byte("slow read"))); err != nil {
		t.Fatal(err)
	}
	select {
	case <-entered:
	case <-time.After(time.Second):
		t.Fatal("read handler did not start")
	}
	sent := make(chan error, 1)
	go func() { _, err := client.Write(callFrame(20, program, 2, []byte("metadata"))); sent <- err }()
	// A serial server cannot read this second call until the first finishes.
	select {
	case err := <-sent:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(400 * time.Millisecond):
		t.Fatal("metadata queued behind blocked READ")
	}
	xid, b := readFrame(t, client)
	if xid != 20 || string(b) != "metadata" {
		t.Fatalf("wrong reply: xid=%d body=%q", xid, b)
	}
}

func TestConcurrentRepliesKeepTheirBodiesAndFrames(t *testing.T) {
	handler := func(ctx context.Context, w *response, h Handler) error {
		b, err := io.ReadAll(w.req.Body)
		if err != nil {
			return err
		}
		time.Sleep(time.Duration(w.req.xid%4) * time.Millisecond)
		return w.Write(b)
	}
	client, program, _, _ := transport(t, map[uint32]HandleFunc{1: handler})
	const count = 40
	sent := make(chan error, 1)
	go func() {
		for i := uint32(1); i <= count; i++ {
			var b [4]byte
			binary.BigEndian.PutUint32(b[:], i)
			if _, err := client.Write(callFrame(i, program, 1, b[:])); err != nil {
				sent <- err
				return
			}
		}
		sent <- nil
	}()
	seen := map[uint32]bool{}
	for i := 0; i < count; i++ {
		xid, b := readFrame(t, client)
		if len(b) != 4 || binary.BigEndian.Uint32(b) != xid || seen[xid] {
			t.Fatalf("interleaved/mismatched response xid=%d body=%x", xid, b)
		}
		seen[xid] = true
	}
	if err := <-sent; err != nil {
		t.Fatal(err)
	}
}

func TestInflightHandlersAreBounded(t *testing.T) {
	var active, peak atomic.Int32
	entered := make(chan struct{}, 32)
	release := make(chan struct{})
	defer close(release)
	handler := func(ctx context.Context, w *response, h Handler) error {
		n := active.Add(1)
		defer active.Add(-1)
		for {
			old := peak.Load()
			if n <= old || peak.CompareAndSwap(old, n) {
				break
			}
		}
		entered <- struct{}{}
		select {
		case <-release:
		case <-ctx.Done():
			return ctx.Err()
		}
		return w.Write(nil)
	}
	client, program, done, cancel := transport(t, map[uint32]HandleFunc{1: handler})
	go func() {
		for i := uint32(0); i < 24; i++ {
			if _, err := client.Write(callFrame(i, program, 1, nil)); err != nil {
				return
			}
		}
	}()
	for i := 0; i < 8; i++ {
		select {
		case <-entered:
		case <-time.After(time.Second):
			t.Fatal("worker slots did not fill")
		}
	}
	time.Sleep(30 * time.Millisecond)
	if peak.Load() != 8 {
		t.Fatalf("inflight peak=%d, want8", peak.Load())
	}
	cancel()
	client.Close()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("cancel did not retire bounded workers")
	}
	if active.Load() != 0 {
		t.Fatal("handler remains after cancel")
	}
}

func TestBadFrameRefusesBeforeHandler(t *testing.T) {
	for _, n := range []uint32{39 | (1 << 31), (1 << 20) + 1 | (1 << 31), 40} {
		t.Run(fmt.Sprintf("frame_%08x", n), func(t *testing.T) {
			var calls atomic.Int32
			handler := func(context.Context, *response, Handler) error { calls.Add(1); return nil }
			client, _, done, _ := transport(t, map[uint32]HandleFunc{1: handler})
			var h [4]byte
			binary.BigEndian.PutUint32(h[:], n)
			if _, err := client.Write(h[:]); err != nil {
				t.Fatal(err)
			}
			select {
			case <-done:
			case <-time.After(time.Second):
				t.Fatal("bad frame did not close")
			}
			if calls.Load() != 0 {
				t.Fatal("bad frame dispatched")
			}
		})
	}
}

func TestPartialBodyRefusesBeforeHandler(t *testing.T) {
	var calls atomic.Int32
	handler := func(context.Context, *response, Handler) error { calls.Add(1); return nil }
	client, program, done, _ := transport(t, map[uint32]HandleFunc{1: handler})
	frame := callFrame(1, program, 1, []byte("incomplete body"))
	if _, err := client.Write(frame[:46]); err != nil {
		t.Fatal(err)
	}
	client.Close()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("partial body did not retire")
	}
	if calls.Load() != 0 {
		t.Fatal("partial body dispatched")
	}
}

func TestIdleConnectionRetiresOnContextCancel(t *testing.T) {
	client, _, done, cancel := transport(t, map[uint32]HandleFunc{})
	cancel()
	client.Close()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("idle reader did not retire")
	}
}

// A client may finish sending calls while it still expects their replies.
func TestTCPHalfCloseFlushesAcceptedResponse(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	program := atomic.AddUint32(&testProgram, 1)
	entered, release := make(chan struct{}), make(chan struct{})
	handler := func(ctx context.Context, w *response, h Handler) error {
		close(entered)
		select {
		case <-release:
		case <-ctx.Done():
			return ctx.Err()
		}
		return w.Write([]byte("after half close"))
	}
	if err := RegisterMessageHandler(program, 1, handler); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() {
		defer close(done)
		socket, err := listener.Accept()
		if err != nil {
			return
		}
		(&Server{}).newConn(socket).serve(ctx)
	}()
	client, err := net.DialTCP("tcp", nil, listener.Addr().(*net.TCPAddr))
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	if _, err := client.Write(callFrame(99, program, 1, nil)); err != nil {
		t.Fatal(err)
	}
	select {
	case <-entered:
	case <-time.After(time.Second):
		t.Fatal("handler not entered")
	}
	if err := client.CloseWrite(); err != nil {
		t.Fatal(err)
	}
	close(release)
	xid, body := readFrame(t, client)
	if xid != 99 || string(body) != "after half close" {
		t.Fatalf("lost reply xid=%d body=%q", xid, body)
	}
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("half-close did not retire")
	}
}
