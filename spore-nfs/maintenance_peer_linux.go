//go:build linux

package main

import (
	"golang.org/x/sys/unix"
	"net"
	"syscall"
)

func rootPeer(c *net.UnixConn) bool {
	raw, err := c.SyscallConn()
	if err != nil {
		return false
	}
	allowed := false
	err = raw.Control(func(fd uintptr) {
		u, e := syscall.GetsockoptUcred(int(fd), syscall.SOL_SOCKET, syscall.SO_PEERCRED)
		allowed = e == nil && u.Uid == 0
	})
	return err == nil && allowed
}

// Python time.monotonic_ns and this helper share CLOCK_MONOTONIC in the
// container time namespace; a carrier must preserve that namespace join.
func monotonicNanos() (int64, error) {
	var ts unix.Timespec
	if err := unix.ClockGettime(unix.CLOCK_MONOTONIC, &ts); err != nil {
		return 0, err
	}
	return ts.Nano(), nil
}
