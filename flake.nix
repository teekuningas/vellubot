{
  description = "A Python development shell for an IRC bot";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-23.05";

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};

      myPythonEnv = pkgs.python39.withPackages (ps: [
        ps.requests
        ps.irc
        ps.beautifulsoup4
        ps.lxml
        ps.black
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
