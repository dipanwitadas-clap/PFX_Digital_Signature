# Copyright (c) 2026, Clapgrow and contributors
# For license information, please see license.txt

import frappe
from frappe.tests import IntegrationTestCase

from pfx_sign.api import log_sign_event


class IntegrationTestPfxSignLog(IntegrationTestCase):
	def test_log_sign_event_creates_entry(self):
		name = log_sign_event(
			reference_doctype="User",
			reference_name="Administrator",
			status="Failed",
			error_message="Incorrect password.",
			commit=False,
		)
		log = frappe.get_doc("Pfx Sign Log", name)
		self.assertEqual(log.signed_by, frappe.session.user)
		self.assertEqual(log.status, "Failed")
		self.assertEqual(log.reference_name, "Administrator")
