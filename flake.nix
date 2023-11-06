{
  description = "A Python development shell for an IRC bot";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-23.05";

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};

      irc = pkgs.python39Packages.irc.overridePythonAttrs (oldAttrs: {
        version = "20.3.0";
        name = "irc-20.3.0";
        pname = "irc";
        src = pkgs.fetchPypi {
          pname = "irc";
          version = "20.3.0";
          hash = "sha256-JFteqYqwAlZnYx53alXjGRfmDvcIxgEC8hmLyfURMjY=";
        };
      });

      myPythonEnv = pkgs.python39.withPackages (ps: [
        ps.requests
        irc
        ps.beautifulsoup4
        ps.lxml
        ps.black
        (ps.mypy.overridePythonAttrs (oldAttrs: { doCheck = false; }))
      ]);

      botScript = pkgs.writeShellScriptBin "bot" ''
        ${myPythonEnv}/bin/python ${./bot.py}
      '';
    in
    {
      devShell.${system} = pkgs.mkShell {
        buildInputs = [
          myPythonEnv
        ];
      };

      packages.${system}.bot = botScript;
    };
}
