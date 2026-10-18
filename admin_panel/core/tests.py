"""Security and schema integration tests for the Django report projection."""

from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test import RequestFactory, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from app.models import ReviewReport as ApiReviewReport

from .admin import ReviewReportAdmin
from .models import ReviewReport


@override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
)
class ReviewReportTests(TransactionTestCase):
    """Unmanaged SQLModel tables must be explicitly provided in a test DB."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        with connection.schema_editor() as editor:
            editor.create_model(ReviewReport)

    @classmethod
    def tearDownClass(cls) -> None:
        with connection.schema_editor() as editor:
            editor.delete_model(ReviewReport)
        super().tearDownClass()

    def setUp(self) -> None:
        # Django flush deliberately ignores tables with managed=False.
        ReviewReport.objects.all().delete()
        self.staff = User.objects.create_user("review-staff", is_staff=True)
        content_type = ContentType.objects.get_for_model(ReviewReport)
        permission, _ = Permission.objects.get_or_create(
            content_type=content_type,
            codename="view_reviewreport",
            defaults={"name": "Can view review report"},
        )
        self.staff.user_permissions.add(permission)
        self.report = ReviewReport.objects.create(
            id=1,
            project_name='<img src=x onerror="alert(1)">',
            file_path="project/example.py",
            content_type="code",
            summary="Example summary",
            review_result="<script>alert('model-output')</script>",
            status="completed",
            created_at=timezone.now(),
        )

    def test_anonymous_is_redirected_to_login(self) -> None:
        response = self.client.get(reverse("report_list"))
        self.assertRedirects(response, "/admin/login/?next=/stats/")
        detail = self.client.get(reverse("report_detail", args=[1]))
        self.assertRedirects(detail, "/admin/login/?next=/stats/1/")

    def test_authenticated_user_without_permission_is_denied(self) -> None:
        user = User.objects.create_user("ordinary-user")
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse("report_list")).status_code, 403)
        self.assertEqual(self.client.get(reverse("report_detail", args=[1])).status_code, 403)

    def test_staff_without_permission_is_denied(self) -> None:
        staff = User.objects.create_user("unprivileged-staff", is_staff=True)
        self.client.force_login(staff)
        self.assertEqual(self.client.get(reverse("report_list")).status_code, 403)

    def test_nonstaff_with_permission_is_denied(self) -> None:
        self.staff.is_staff = False
        self.staff.save(update_fields=["is_staff"])
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse("report_list")).status_code, 403)

    def test_authorized_report_content_is_escaped(self) -> None:
        self.client.force_login(self.staff)
        response = self.client.get(reverse("report_list"))
        self.assertContains(response, "История ревью кода")
        self.assertContains(response, "&lt;img")
        self.assertNotContains(response, "model-output")
        detail = self.client.get(reverse("report_detail", args=[1]))
        self.assertContains(detail, "&lt;script&gt;")
        self.assertNotContains(detail, "<script>")
        self.assertNotContains(response, "<script>")
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertIn("default-src 'none'", response["Content-Security-Policy"])

    def test_admin_cannot_mutate_api_reports(self) -> None:
        request = RequestFactory().get("/admin/")
        request.user = self.staff
        model_admin = ReviewReportAdmin(ReviewReport, AdminSite())
        self.assertFalse(model_admin.has_add_permission(request))
        self.assertFalse(model_admin.has_change_permission(request, self.report))
        self.assertFalse(model_admin.has_delete_permission(request, self.report))
        self.assertTrue(model_admin.has_view_permission(request, self.report))

    def test_schema_matches_shared_table(self) -> None:
        self.assertEqual(ReviewReport.objects.get(pk=1).file_path, "project/example.py")
        self.assertFalse(ReviewReport._meta.managed)
        self.assertEqual(ReviewReport._meta.db_table, ApiReviewReport.__tablename__)
        self.assertEqual(
            {field.column for field in ReviewReport._meta.concrete_fields},
            set(ApiReviewReport.model_fields),
        )

    def test_health_is_public_and_does_not_expose_configuration(self) -> None:
        response = self.client.get(reverse("health"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        self.assertEqual(response["Cache-Control"], "no-store")
