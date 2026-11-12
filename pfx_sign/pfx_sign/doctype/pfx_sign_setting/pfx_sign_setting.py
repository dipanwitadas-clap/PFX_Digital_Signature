# Copyright (c) 2026, Clapgrow and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class PfxSignSetting(Document):
	def validate(self):
		if (
			not self.is_default
			and not frappe.flags.in_migrate
			and not frappe.flags.in_install
		):
			if not frappe.db.exists("Pfx Sign Setting", dict(is_default=1)):
				self.is_default = 1

	def on_update(self):
		self.set_as_default()

	def set_as_default(self):
		from frappe.utils import set_default

		if self.is_default:
			frappe.db.set_value(
				"Pfx Sign Setting", {"name": ["!=", self.name]}, "is_default", 0
			)
			set_default("pfx_sign_setting", self.name)
		else:
			frappe.defaults.clear_default("pfx_sign_setting", self.name)

	def on_trash(self):
		frappe.defaults.clear_default("pfx_sign_setting", self.name)
