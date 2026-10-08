variable "project_id" {
  description = "GCP project to deploy into."
  type        = string
}

variable "region" {
  description = "Region for Cloud Run, Artifact Registry and Cloud SQL."
  type        = string
  default     = "europe-west2"
}

variable "service_name" {
  description = "Cloud Run service name."
  type        = string
  default     = "agent-runtime"
}

variable "image" {
  description = "Full image reference, e.g. europe-west2-docker.pkg.dev/PROJECT/agent-runtime/api:v0.1.0. The release workflow sets this."
  type        = string
}

variable "min_instances" {
  description = "Minimum Cloud Run instances (0 = scale to zero)."
  type        = number
  default     = 0
}

variable "max_instances" {
  description = "Maximum Cloud Run instances."
  type        = number
  default     = 3
}

variable "allow_unauthenticated" {
  description = "Grant roles/run.invoker to allUsers. Keep false unless the service has its own auth."
  type        = bool
  default     = false
}

variable "enable_cloudsql" {
  description = "Create a small Cloud SQL Postgres instance for checkpoints and memory. If false, pass an external DSN via the pg-dsn secret."
  type        = bool
  default     = true
}

variable "db_tier" {
  description = "Cloud SQL machine tier."
  type        = string
  default     = "db-f1-micro"
}

variable "deletion_protection" {
  description = "Protect the Cloud SQL instance from terraform destroy."
  type        = bool
  default     = true
}

variable "otel_endpoint" {
  description = "OTLP/HTTP endpoint for metrics and traces (empty = console exporter)."
  type        = string
  default     = ""
}
