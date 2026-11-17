from django.urls import path

from .views.auth import (
    signup_api,
    verify_otp_api,
    resend_otp,
    login_api,
    logout_api,
    current_user_api,
)
from .views.queue import (
    join_queue_api,
    my_queue_api,
    leave_queue_api,
    customer_services_api,
    customer_history_api,
    customer_profile_api,
    customer_notifications_api,
)
from .views.staff import (
    staff_queue_api,
    staff_history_api,
    call_next_api,
    complete_token_api,
    skip_token_api,
    start_service_api,
)
from .views.push import customer_push_subscriptions_api
from .views.admin import (
    admin_customers_api,
    admin_staff_api,
    create_staff_api,
    delete_staff_api,
    set_customer_active_api,
    set_staff_active_api,
    admin_queue_history_api,
)

from user.views.admin import (
    get_organization_api,
    update_organization_api,
)

from user.views.admin import (
    get_services_api,
    create_service_api,
    update_service_api,
    toggle_service_api,
    delete_service_api,
)

urlpatterns = [
    # Authentication
    path("signup/", signup_api, name="signup_api"),
    path("verify-otp/", verify_otp_api, name="verify_otp_api"),
    path("resend-otp/", resend_otp, name="resend_otp_api"),
    path("login/", login_api, name="login_api"),
    path("logout/", logout_api, name="logout_api"),
    path("current-user/", current_user_api, name="current_user_api"),
    # Customer queue
    path("join-queue/", join_queue_api, name="join_queue_api"),
    path("my-queue/", my_queue_api, name="my_queue_api"),
    path("leave-queue/", leave_queue_api, name="leave_queue_api"),
    path("customer-services/", customer_services_api, name="customer_services_api"),
    path("customer-history/", customer_history_api, name="customer_history_api"),
    path("customer-profile/", customer_profile_api, name="customer_profile_api"),
    path("customer-notifications/", customer_notifications_api, name="customer_notifications_api"),
    path(
        "customer-push-subscriptions/",
        customer_push_subscriptions_api,
        name="customer_push_subscriptions_api",
    ),
    # Staff queue
    path("staff-queue/", staff_queue_api, name="staff_queue_api"),
    path("staff-history/", staff_history_api, name="staff_history_api"),
    path("call-next/", call_next_api, name="call_next_api"),
    path("start-service/", start_service_api, name="start_service_api"),
    path("complete-token/", complete_token_api, name="complete_token_api"),
    path("skip-token/", skip_token_api, name="skip_token_api"),
    # Admin
    path("admin-customers/", admin_customers_api, name="admin_customers_api"),
    path(
        "customers/<int:customer_id>/status/",
        set_customer_active_api,
        name="set_customer_active_api",
    ),
    path("admin-staff/", admin_staff_api, name="admin_staff_api"),
    path("create-staff/", create_staff_api, name="create_staff_api"),
    path(
        "staff/<int:staff_id>/status/",
        set_staff_active_api,
        name="set_staff_active_api",
    ),
    path("staff/<int:staff_id>/delete/", delete_staff_api, name="delete_staff_api"),
    path(
        "admin/queue-history/<str:status>/",
        admin_queue_history_api,
        name="admin_queue_history_api",
    ),
    path("organization/", get_organization_api, name="get-organization"),
    path("organization/update/", update_organization_api, name="update-organization"),
    # Service Management
    path("services/", get_services_api, name="get-services"),
    path("services/create/", create_service_api, name="create-service"),
    path(
        "services/<int:service_id>/update/", update_service_api, name="update-service"
    ),
    path(
        "services/<int:service_id>/toggle/", toggle_service_api, name="toggle-service"
    ),
    path(
        "services/<int:service_id>/delete/", delete_service_api, name="delete-service"
    ),
]
