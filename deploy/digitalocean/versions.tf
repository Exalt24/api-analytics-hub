terraform {
  required_version = ">= 1.5"

  required_providers {
    digitalocean = {
      source  = "digitalocean/digitalocean"
      version = "~> 2.0"
    }
  }
}

# The API token is never written down in this folder. The provider reads DIGITALOCEAN_TOKEN from the environment,
# so a plan or an apply needs `export DIGITALOCEAN_TOKEN=...` in the shell that runs it and nothing else.
provider "digitalocean" {}
