{
  description = "api-analytics-hub: a dev shell with Python 3.13, PostgreSQL 17 with pgvector, git and Node 22, and a command that runs the fast tests in it";

  # One input. flake.lock pins the exact nixpkgs revision, so the interpreter, the Postgres server and client, git and Node are the same on every machine.
  # The Python packages are not from nixpkgs: they are installed from requirements.txt into a virtualenv, the way the CI job does it (see docs/nix.md).
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
      forAll = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});

      # The fast test command: the tests that need no Postgres, no browser and no stripe-mock. 470 tests, a few seconds once the virtualenv exists.
      # tests/test_flake_lock.py is in the list, so the lock check runs with the rest. A test that names a file here which does not exist fails (test_flake_lock.py).
      fastTestArgs = "tests/test_flake_lock.py tests/test_crypto.py tests/test_registry.py tests/test_tokens.py tests/test_shopify_webhooks.py tests/test_alert_policy.py tests/test_processed_at_bucketing.py tests/test_secret_files.py tests/test_vendor_common.py tests/test_billing_signature.py tests/test_billing_proration.py tests/test_no_secrets_in_repo.py tests/test_doc_links.py";

      toolsFor = pkgs: {
        python = pkgs.python313;
        postgres = pkgs.postgresql_17.withPackages (ps: [ ps.pgvector ]);
        packages = [
          pkgs.python313
          (pkgs.postgresql_17.withPackages (ps: [ ps.pgvector ]))
          pkgs.git
          pkgs.nodejs_22
          pkgs.curl
          pkgs.coreutils
        ];
        # Binary wheels from PyPI (lxml, cryptography, pillow, psycopg-binary) look for libstdc++ and zlib in the usual places, which a Nix system does not have.
        libs = pkgs.lib.optionals pkgs.stdenv.isLinux [ pkgs.stdenv.cc.cc.lib pkgs.zlib ];
      };

      # Makes .venv-nix from requirements.txt the first time, and again when requirements.txt changes. Needs the network the first time.
      bootstrap = pkgs: ''
        ${pkgs.lib.optionalString pkgs.stdenv.isLinux ''export LD_LIBRARY_PATH="${pkgs.lib.makeLibraryPath (toolsFor pkgs).libs}''${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"''}
        hub_venv="''${HUB_VENV:-$PWD/.venv-nix}"
        hub_want="$(sha256sum requirements.txt | cut -d' ' -f1)"
        if [ ! -x "$hub_venv/bin/python" ] || [ "$(cat "$hub_venv/.requirements.sha256" 2>/dev/null || true)" != "$hub_want" ]; then
          echo "building $hub_venv from requirements.txt" >&2
          python -m venv --clear "$hub_venv"
          "$hub_venv/bin/pip" install --disable-pip-version-check --quiet -r requirements.txt
          echo "$hub_want" > "$hub_venv/.requirements.sha256"
        fi
        export VIRTUAL_ENV="$hub_venv"
        export PATH="$hub_venv/bin:$PATH"
      '';

      fastTests = pkgs: pkgs.writeShellApplication {
        name = "hub-fast-tests";
        runtimeInputs = (toolsFor pkgs).packages;
        text = ''
          ${bootstrap pkgs}
          exec python -m pytest ${fastTestArgs} -q "$@"
        '';
      };
    in
    {
      devShells = forAll (pkgs: {
        default = pkgs.mkShell {
          packages = (toolsFor pkgs).packages ++ [ (fastTests pkgs) ];
          shellHook = ''
            ${bootstrap pkgs}
            echo "api-analytics-hub dev shell: $(python --version), postgres $(postgres --version | cut -d' ' -f3), git $(git --version | cut -d' ' -f3), node $(node --version)" >&2
          '';
        };
      });

      packages = forAll (pkgs: {
        fast-tests = fastTests pkgs;
        default = fastTests pkgs;
      });

      apps = forAll (pkgs: {
        fast-tests = { type = "app"; program = "${fastTests pkgs}/bin/hub-fast-tests"; meta.description = "Run the tests that need no Postgres, browser or stripe-mock"; };
        default = { type = "app"; program = "${fastTests pkgs}/bin/hub-fast-tests"; meta.description = "Run the tests that need no Postgres, browser or stripe-mock"; };
      });

      checks = forAll (pkgs:
        let t = toolsFor pkgs; in {
          # The versions the repo documents, checked from the built tools and not from the attribute names.
          toolchain = pkgs.runCommand "hub-toolchain-check" { nativeBuildInputs = t.packages; } ''
            python --version | grep -q '^Python 3\.13\.' || { echo "python is not 3.13"; python --version; exit 1; }
            postgres --version | grep -q ' 17\.' || { echo "postgres is not 17"; postgres --version; exit 1; }
            ext="$(dirname "$(dirname "$(command -v postgres)")")/share/postgresql/extension"
            test -f "$ext/vector.control" || { echo "pgvector is missing from $ext"; exit 1; }
            node --version | grep -q '^v22\.' || { echo "node is not 22"; exit 1; }
            git --version > /dev/null
            { python --version; postgres --version; grep default_version "$ext/vector.control"; node --version; git --version; } > $out
          '';
          # Building the test command runs shellcheck over it.
          fast-tests = fastTests pkgs;
        });
    };
}
