//go:build linux

package main

import (
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
