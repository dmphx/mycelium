//go:build !linux

package main

import "net"

// Maintenance control is Linux-only and refuses every peer elsewhere.
func rootPeer(c *net.UnixConn) bool { return false }
