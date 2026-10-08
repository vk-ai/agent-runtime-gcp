output "service_url" {
  description = "Cloud Run URL."
  value       = google_cloud_run_v2_service.api.uri
}

output "latest_ready_revision" {
  description = "Latest ready revision (the release workflow's rollback target is the one before it)."
  value       = google_cloud_run_v2_service.api.latest_ready_revision
}

output "artifact_registry" {
  description = "Docker repository path for images."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}

output "runtime_service_account" {
  value = google_service_account.runtime.email
}

output "cloudsql_connection_name" {
  description = "Use in the DSN: postgresql://USER:PASS@/agentrt?host=/cloudsql/<connection_name>"
  value       = var.enable_cloudsql ? google_sql_database_instance.pg[0].connection_name : null
}
