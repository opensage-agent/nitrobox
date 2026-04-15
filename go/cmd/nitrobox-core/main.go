package main

import (
	"encoding/json"
	"fmt"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"

	nbxbuildkit "github.com/opensage-agent/nitrobox/go/internal/buildkit"
	"github.com/spf13/cobra"
)

func main() {
	signal.Ignore(syscall.SIGPIPE)

	rootCmd := &cobra.Command{
		Use:           "nitrobox-core",
		Short:         "nitrobox image management (containers/storage + buildah)",
		SilenceUsage:  true,
		SilenceErrors: true,
	}

	// -- BuildKit commands ------------------------------------------------

	rootCmd.AddCommand(&cobra.Command{
		Use:   "buildkit-serve",
		Short: "Run embedded buildkitd (manages rootless userns via rootlesskit)",
		RunE: func(cmd *cobra.Command, args []string) error {
			var req struct {
				RootDir string `json:"root_dir"`
			}
			// Read config from env (survives re-exec) or stdin
			if configEnv := os.Getenv("_NITROBOX_BUILDKIT_CONFIG"); configEnv != "" {
				json.Unmarshal([]byte(configEnv), &req)
			} else {
				if err := readJSON(&req); err != nil {
					return err
				}
				reqJSON, _ := json.Marshal(req)
				os.Setenv("_NITROBOX_BUILDKIT_CONFIG", string(reqJSON))
			}

			rootDir := req.RootDir
			if rootDir == "" {
				rootDir = nbxbuildkit.DefaultRootDir()
			}

			// Preserve Docker config path before entering userns
			// (HOME changes to /root inside userns)
			if os.Getenv("DOCKER_CONFIG") == "" {
				home, _ := os.UserHomeDir()
				dockerCfg := filepath.Join(home, ".docker")
				if _, err := os.Stat(filepath.Join(dockerCfg, "config.json")); err == nil {
					os.Setenv("DOCKER_CONFIG", dockerCfg)
				}
			}

			if nbxbuildkit.IsRootlessChild() {
				// We're the rootlesskit child — complete userns setup
				// and exec buildkit-serve-inner (the actual server)
				return nbxbuildkit.RunChild([]string{"buildkit-serve-inner"})
			}

			// We're the original parent — create userns via rootlesskit
			// and re-exec ourselves as child
			return nbxbuildkit.RunParent(rootDir, []string{"buildkit-serve"})
		},
	})

	rootCmd.AddCommand(&cobra.Command{
		Use:    "buildkit-serve-inner",
		Short:  "Internal: run buildkitd inside rootlesskit userns",
		Hidden: true,
		RunE: func(cmd *cobra.Command, args []string) error {
			// We're inside the userns. Read config from env.
			var req struct {
				RootDir string `json:"root_dir"`
			}
			if configEnv := os.Getenv("_NITROBOX_BUILDKIT_CONFIG"); configEnv != "" {
				json.Unmarshal([]byte(configEnv), &req)
			}
			rootDir := req.RootDir
			if rootDir == "" {
				rootDir = nbxbuildkit.DefaultRootDir()
			}

			srv := nbxbuildkit.NewServer(rootDir)
			socketPath, err := srv.Start()
			if err != nil {
				return err
			}

			// Write socket info to well-known file
			infoPath := filepath.Join(rootDir, "server.json")
			infoJSON, _ := json.Marshal(map[string]string{
				"socket_path": socketPath,
				"root_dir":    rootDir,
			})
			os.WriteFile(infoPath, infoJSON, 0644)

			// Start the nitrobox handler (JSON-over-Unix-socket)
			handlerPath, err := srv.StartHandler()
			if err != nil {
				srv.Stop()
				return fmt.Errorf("start handler: %w", err)
			}

			// Write server info (overwrite earlier file)
			infoPath = filepath.Join(rootDir, "server.json")
			infoJSON, _ = json.Marshal(map[string]string{
				"socket_path":  socketPath,
				"handler_path": handlerPath,
				"root_dir":     rootDir,
			})
			os.WriteFile(infoPath, infoJSON, 0644)

			// Signal readiness
			readyPath := filepath.Join(rootDir, "ready")
			os.WriteFile(readyPath, []byte("1"), 0644)
			fmt.Fprintf(os.Stderr, "buildkit-serve: ready at %s (handler: %s)\n", socketPath, handlerPath)

			// Block until signal
			sigCh := make(chan os.Signal, 1)
			signal.Notify(sigCh, syscall.SIGTERM, syscall.SIGINT)
			<-sigCh

			srv.Stop()
			return nil
		},
	})

	if err := rootCmd.Execute(); err != nil {
		fmt.Fprintf(os.Stderr, "error: %v\n", err)
		os.Exit(1)
	}
}

func readJSON(v any) error {
	return json.NewDecoder(os.Stdin).Decode(v)
}

func writeJSON(v any) error {
	return json.NewEncoder(os.Stdout).Encode(v)
}
