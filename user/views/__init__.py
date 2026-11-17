"""View package for QueueLess.

Import API views from their specific modules. The project URLconf also
imports the admin dashboard from this package for backwards compatibility.
"""
from .admin import admin_dashboard
