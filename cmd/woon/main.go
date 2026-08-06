package main

import (
	"fmt"
	"os"

	"github.com/woonyong-kr/woon-core/internal/app"
)

func main() {
	if err := app.Run(os.Args[1:], os.Stdout, os.Stderr); err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
}
