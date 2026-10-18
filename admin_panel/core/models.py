"""Read-only Django projection of the API-owned SQLModel table."""

from django.db import models


class ReviewReport(models.Model):
    id = models.IntegerField(primary_key=True)
    project_name = models.TextField()
    file_path = models.TextField()
    content_type = models.TextField()
    summary = models.TextField()
    review_result = models.TextField()
    status = models.TextField()
    created_at = models.DateTimeField()
    owner_id = models.IntegerField(null=True)
    job_id = models.TextField(null=True)

    class Meta:
        managed = False
        db_table = "reviewreport"
        default_permissions = ("view",)

    def __str__(self) -> str:
        return f"{self.project_name} — {self.status}"
