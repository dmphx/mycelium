//go:build linux

package main

import (
	"net"
	"os"
	"path/filepath"
	"testing"
)

func TestMaintenancePeerUsesKernelUID(t *testing.T) {
	path := filepath.Join(t.TempDir(), "control.sock")
	l, err := net.ListenUnix("unix", &net.UnixAddr{Name: path, Net: "unix"})
	if err != nil {
		t.Fatal(err)
	}
	defer l.Close()
	client, err := net.DialUnix("unix", nil, &net.UnixAddr{Name: path, Net: "unix"})
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	server, err := l.AcceptUnix()
	if err != nil {
		t.Fatal(err)
	}
	defer server.Close()
	if rootPeer(server) != (os.Geteuid() == 0) {
		t.Fatal("control peer was not checked against kernel UID0")
	}
}
