from django.contrib import admin
from django.urls import include, path

from .views import (
    landing,
    login,
    customerDashboard,
    joinqueue,
    notify,
    profile,
    queuestatus,
    staffdashboard,
    staffalert,
    staffcallnext,
    staffprofile,
    staffqueue,
)

from user.views.admin import admin_dashboard

urlpatterns = [
    # =====================================================
    # USER API
    # =====================================================
    path("api/user/", include("user.urls")),
    # =====================================================
    # DJANGO ADMIN
    # =====================================================
    path("admin/", admin.site.urls),
    # =====================================================
    # LOGIN
    # =====================================================
    path("", landing, name="home"),
    path("login.html/", login, name="login"),
    # =====================================================
    # CUSTOMER
    # =====================================================
    path("customerDashboard.html/", customerDashboard, name="customerDashboard"),
    path("joinqueue.html/", joinqueue, name="joinqueue"),
    path("notifications.html/", notify, name="notify"),
    path("profile.html/", profile, name="profile"),
    path("queuestatus.html/", queuestatus, name="queuestatus"),
    # =====================================================
    # STAFF
    # =====================================================
    path("staffdashboard.html/", staffdashboard, name="staffdashboard"),
    path("staffalerts.html/", staffalert, name="staffalert"),
    path("staffcallnext.html/", staffcallnext, name="staffcallnext"),
    path("staffprofile.html/", staffprofile, name="staffprofile"),
    path("staffqueue.html/", staffqueue, name="staffqueue"),
    # =====================================================
    # ADMIN
    # =====================================================
    path("admindashboard.html/", admin_dashboard, name="admindashboard"),
]
