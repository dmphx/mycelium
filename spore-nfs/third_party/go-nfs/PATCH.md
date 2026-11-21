# Local transport patch

Upstream: github.com/willscott/go-nfs v0.0.4. The original MIT LICENSE is retained. UPSTREAM.json records the original file hashes and module checksum. Only conn.go differs from the copied upstream implementation; conn_concurrent_test.go adds transport regression tests.

The patch copies a complete RPC request before dispatch, limits a connection to eight active handlers and a one MiB request frame, and serializes complete response frames through one acknowledged writer. A clean TCP read half-close drains accepted responses. Cancellation and transport errors close the socket and retire context-aware handlers. Responses retain their RPC transaction IDs and may finish out of request order.

This removes a slow READ's unconditional ordering barrier for later metadata calls on the same connection. Eight blocked handlers can still fill all slots; this is bounded concurrency, not a latency guarantee. Provider/CDN and cache policies, NFS procedure implementations, and the read-only Spore filesystem are unchanged.

Run tests in this directory with `go test -race ./...`; also run the Spore module's tests. The metadata-overtaking regression must fail against the original upstream conn.go. upstream-transport.diff.json records the exact transport delta.
