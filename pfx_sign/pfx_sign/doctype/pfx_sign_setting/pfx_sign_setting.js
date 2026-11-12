// Copyright (c) 2026, Clapgrow and contributors
// For license information, please see license.txt

frappe.ui.form.on("Pfx Sign Setting", {
	refresh(frm) {
		if (!frm.is_new() && !frm.doc.is_default) {
			frm.add_custom_button(__("Set as Default"), () => {
				frm.set_value("is_default", 1);
				frm.save();
			});
		}
	},
});
