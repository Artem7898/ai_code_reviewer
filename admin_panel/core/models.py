from django.db import models

class ReviewReport(models.Model):
    id = models.IntegerField(primary_key=True)
    project_name = models.CharField(max_length=100)
    file_path = models.TextField()
    content_type = models.CharField(max_length=10)
    summary = models.TextField()
    review_result = models.TextField()
    status = models.CharField(max_length=20)
    created_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = 'reviewreport'

    def __str__(self):
        return f"{self.project_name} - {self.status}"