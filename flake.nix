{
  description = "A Python development shell for an IRC bot";

  inputs.nixpkgs.url = "nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};

      irc = pkgs.python311Packages.irc.overridePythonAttrs (oldAttrs: {
        version = "20.3.0";
        name = "irc-20.3.0";
        pname = "irc";
        src = pkgs.fetchPypi {
          pname = "irc";
          version = "20.3.0";
          hash = "sha256-JFteqYqwAlZnYx53alXjGRfmDvcIxgEC8hmLyfURMjY=";
        };
      });

      myPythonEnv = pkgs.python311.withPackages (ps: [
        ps.requests
        irc
        ps.beautifulsoup4
        ps.lxml
        ps.openai
        ps.tiktoken
        ps.black
        (ps.mypy.overridePythonAttrs (oldAttrs: { doCheck = false; }))
      ]);

      vellubot = pkgs.python311Packages.buildPythonApplication rec {
        pname = "vellubot";
        version = "0.1.0";
        src = ./.;  # Use the current directory as the source

        propagatedBuildInputs = [
          myPythonEnv
        ];

        # This is necessary if your package has dependencies that need to be compiled
        buildInputs = with pkgs.python311Packages; [ setuptools ];
      };
    in
    {
      devShell.${system} = pkgs.mkShell {
        buildInputs = [
          vellubot
        ];
      };
      packages.${system}.bot = vellubot;
    };
}
