locals {
  services = [
    "run.googleapis.com",
    "artifactregistry.googleapis.com",
    "secretmanager.googleapis.com",
    "sqladmin.googleapis.com",
  ]
  labels = {
    app        = var.service_name
    managed_by = "terraform"
  }
}

resource "google_project_service" "apis" {
  for_each           = toset(local.services)
  service            = each.value
  disable_on_destroy = false
}

resource "google_artifact_registry_repository" "images" {
  location      = var.region
  repository_id = var.service_name
  format        = "DOCKER"
  description   = "Container images for ${var.service_name}"
  labels        = local.labels

  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = 10
    }
  }

  depends_on = [google_project_service.apis]
}

resource "google_service_account" "runtime" {
  account_id   = "${var.service_name}-sa"
  display_name = "${var.service_name} runtime identity"
}

# --- Secrets -----------------------------------------------------------------
# Values are added out of band (gcloud secrets versions add ...) so they never
# land in Terraform state.
resource "google_secret_manager_secret" "pg_dsn" {
  secret_id = "${var.service_name}-pg-dsn"
  labels    = local.labels
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret" "openai_api_key" {
  secret_id = "${var.service_name}-openai-api-key"
  labels    = local.labels
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_iam_member" "runtime_access" {
  for_each = {
    pg_dsn = google_secret_manager_secret.pg_dsn.id
    openai = google_secret_manager_secret.openai_api_key.id
  }
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime.email}"
}

# --- Optional Cloud SQL Postgres -----------------------------------------------
resource "google_sql_database_instance" "pg" {
  count               = var.enable_cloudsql ? 1 : 0
  name                = "${var.service_name}-pg"
  database_version    = "POSTGRES_16"
  region              = var.region
  deletion_protection = var.deletion_protection

  settings {
    tier              = var.db_tier
    availability_type = "ZONAL"
    user_labels       = local.labels
    backup_configuration {
      enabled = true
    }
    ip_configuration {
      ipv4_enabled = true # Cloud Run connects via the Cloud SQL connector socket, not a public allowlist
    }
  }

  depends_on = [google_project_service.apis]
}

resource "google_sql_database" "agentrt" {
  count    = var.enable_cloudsql ? 1 : 0
  name     = "agentrt"
  instance = google_sql_database_instance.pg[0].name
}

resource "google_project_iam_member" "runtime_sql_client" {
  count   = var.enable_cloudsql ? 1 : 0
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.runtime.email}"
}

# --- Cloud Run ---------------------------------------------------------------
resource "google_cloud_run_v2_service" "api" {
  name     = var.service_name
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"
  labels   = local.labels

  template {
    service_account = google_service_account.runtime.email
    labels          = local.labels

    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.max_instances
    }

    dynamic "volumes" {
      for_each = var.enable_cloudsql ? [1] : []
      content {
        name = "cloudsql"
        cloud_sql_instance {
          instances = [google_sql_database_instance.pg[0].connection_name]
        }
      }
    }

    containers {
      image = var.image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle = true
      }

      env {
        name  = "AGENTRT_PERSISTENCE"
        value = "postgres"
      }
      env {
        name  = "AGENTRT_OTEL"
        value = var.otel_endpoint == "" ? "console" : "otlp"
      }
      env {
        name  = "OTEL_EXPORTER_OTLP_ENDPOINT"
        value = var.otel_endpoint
      }
      env {
        name  = "OTEL_SERVICE_NAME"
        value = var.service_name
      }
      env {
        name = "AGENTRT_PG_DSN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.pg_dsn.secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "OPENAI_API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.openai_api_key.secret_id
            version = "latest"
          }
        }
      }

      dynamic "volume_mounts" {
        for_each = var.enable_cloudsql ? [1] : []
        content {
          name       = "cloudsql"
          mount_path = "/cloudsql"
        }
      }

      startup_probe {
        http_get {
          path = "/healthz"
        }
        initial_delay_seconds = 2
        period_seconds        = 3
        failure_threshold     = 10
      }

      liveness_probe {
        http_get {
          path = "/healthz"
        }
        period_seconds = 30
      }
    }
  }

  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }

  # The release workflow shifts traffic between revisions (deploy/rollback)
  # with gcloud, so Terraform must not fight it over image or traffic.
  lifecycle {
    ignore_changes = [template[0].containers[0].image, traffic, client, client_version]
  }

  depends_on = [google_secret_manager_secret_iam_member.runtime_access]
}

resource "google_cloud_run_v2_service_iam_member" "public" {
  count    = var.allow_unauthenticated ? 1 : 0
  name     = google_cloud_run_v2_service.api.name
  location = google_cloud_run_v2_service.api.location
  role     = "roles/run.invoker"
  member   = "allUsers"
}
