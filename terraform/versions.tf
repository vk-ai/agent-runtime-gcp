terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }

  # Remote state (uncomment and set a bucket you own):
  # backend "gcs" {
  #   bucket = "my-tf-state-bucket"
  #   prefix = "agent-runtime"
  # }
}

provider "google" {
  project = var.project_id
  region  = var.region
}
