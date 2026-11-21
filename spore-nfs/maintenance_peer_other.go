//go:build !linux

package main

import (
	"errors"
	"net"
)

// Maintenance control is Linux-only and refuses every peer elsewhere.
func rootPeer(c *net.UnixConn) bool { return false }

func monotonicNanos() (int64, error) {
	return 0, errors.New("maintenance requires Linux monotonic clock")
}
