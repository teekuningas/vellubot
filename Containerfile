# Use an official Nix runtime as a parent image
FROM nixos/nix

# Set the working directory in the container to /app
WORKDIR /app

# Copy the current directory contents into the container at /app
COPY . /app

# Build
RUN nix build --extra-experimental-features 'nix-command flakes' .#bot

# Use the entrypoint script to start your bot
ENTRYPOINT ["./result/bin/bot"]
