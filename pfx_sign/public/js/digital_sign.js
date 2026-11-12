frappe.provide("pfx_sign");

const SIGN_BUTTON_LABEL = __("Sign PDF and Print");

// Cached, deduplicated fetch of the eligible Pfx Sign Settings, per doctype.
pfx_sign.get_eligible_settings = (function () {
	const cache = new Map();
	const in_flight = new Map();

	return function (doctype) {
		if (!doctype) {
			return Promise.resolve([]);
		}

		if (cache.has(doctype)) {
			return Promise.resolve(cache.get(doctype));
		}

		if (in_flight.has(doctype)) {
			return in_flight.get(doctype);
		}

		const promise = frappe
			.call({
				method: "pfx_sign.api.get_eligible_settings",
				args: { doctype },
			})
			.then((r) => {
				const eligible = Array.isArray(r.message) ? r.message : [];
				cache.set(doctype, eligible);
				return eligible;
			})
			.always(() => {
				in_flight.delete(doctype);
			});

		in_flight.set(doctype, promise);

		return promise;
	};
})();

function open_sign_dialog(frm) {
	Promise.all([
		frappe.db.get_list("Letter Head", {
			filters: { disabled: 0 },
			fields: ["name", "is_default"],
			limit: 0,
		}),
		pfx_sign.get_eligible_settings(frm.doctype),
		frappe.db.get_list("Pfx Sign Setting", {
			fields: ["name", "is_default"],
			limit: 0,
		}),
	])
		.then(([rows, eligible_settings, setting_rows]) => {
			const default_setting =
				(
					(setting_rows || []).find(
						(s) => s.is_default && eligible_settings.includes(s.name)
					) || {}
				).name || "";

			// First option is blank = no letter head.
			const letterheads = [""];
			(rows || []).forEach((lh) => {
				if (lh.is_default) {
					letterheads.splice(1, 0, lh.name);
				} else {
					letterheads.push(lh.name);
				}
			});

			const formats = frappe.meta.get_print_formats(frm.doctype);
			const default_format =
				(frappe.get_meta(frm.doctype) || {}).default_print_format ||
				"Standard";
			const default_lh =
				frm.doc.letter_head ||
				((rows || []).find((lh) => lh.is_default) || {}).name ||
				"";

			const d = new frappe.ui.Dialog({
				title: __("Sign PDF and Print"),
				size: "large",
				fields: [
					{
						fieldtype: "Select",
						fieldname: "pfx_sign_setting",
						label: __("Pfx Sign Setting"),
						options: eligible_settings,
						default: eligible_settings.includes(default_setting)
							? default_setting
							: eligible_settings.length === 1
								? eligible_settings[0]
								: "",
						reqd: 1,
					},
					{ fieldtype: "Column Break" },
					{
						fieldtype: "Password",
						fieldname: "dsc_password",
						label: __("DSC Password"),
						reqd: 1,
					},
					{ fieldtype: "Section Break" },
					{
						fieldtype: "Select",
						fieldname: "print_format",
						label: __("Print Format"),
						options: formats,
						default: formats.includes(default_format)
							? default_format
							: "Standard",
						reqd: 1,
						onchange: () => schedule_preview(),
					},
					{ fieldtype: "Column Break" },
					{
						fieldtype: "Select",
						fieldname: "letter_head",
						label: __("Letter Head"),
						options: letterheads,
						default: letterheads.includes(default_lh)
							? default_lh
							: "",
						onchange: () => schedule_preview(),
					},
					{ fieldtype: "Section Break" },
					{ fieldtype: "HTML", fieldname: "preview" },
				],
				primary_action_label: __("Sign PDF and Print"),
				primary_action(values) {
					d.hide();
					const no_letterhead = values.letter_head ? 0 : 1;
					sign_and_print(frm, {
						pfx_sign_setting: values.pfx_sign_setting,
						password: values.dsc_password,
						print_format: values.print_format,
						letterhead: no_letterhead ? "" : values.letter_head,
						no_letterhead: no_letterhead,
					});
				},
			});

			const $preview = d.fields_dict.preview.$wrapper;
			$preview.html(
				`<style>
					.ss-print-preview-wrapper {
						background: var(--gray-200);
						padding: 24px 0;
						max-height: calc(100vh - 240px);
						overflow-y: auto;
						border-radius: var(--border-radius);
					}
					[data-theme="dark"] .ss-print-preview-wrapper {
						background: var(--gray-800);
					}
				</style>
				<div class="ss-print-preview-wrapper">
					<div style="max-width: 8.3in; margin: 0 auto;">
						<iframe class="ss-print-preview" width="100%" frameborder="0"
							scrolling="no"
							style="display:block;border:0;background:#fff;border-radius:var(--border-radius);">
						</iframe>
					</div>
				</div>`
			);

			function render_preview() {
				const vals = d.get_values(true) || {};
				const has_lh = !!vals.letter_head;

				if (d._req) {
					try {
						d._req.abort();
					} catch (e) {
						// ignore
					}
				}

				d._req = frappe.call({
					method: "frappe.www.printview.get_html_and_style",
					args: {
						doc: frm.doctype,
						name: frm.doc.name,
						print_format: vals.print_format || "Standard",
						letterhead: has_lh ? vals.letter_head : "No Letterhead",
						no_letterhead: has_lh ? 0 : 1,
						settings: JSON.stringify(
							has_lh ? { repeat_header_footer: 0 } : {}
						),
					},
					callback: function (r) {
						if (r.exc) {
							return;
						}
						inject_preview(r.message || {});
					},
				});
			}

			const schedule_preview = frappe.utils.debounce(render_preview, 300);

			function inject_preview(out) {
				const iframe = $preview.find("iframe.ss-print-preview")[0];
				if (!iframe) {
					return;
				}

				const idoc =
					iframe.contentDocument || iframe.contentWindow.document;
				const base = frappe.urllib.get_base_url();
				const print_css =
					frappe.assets.bundled_asset("print.bundle.css");
				const body_html =
					out.html ||
					`<div class="text-muted text-center" style="padding:3rem;">${__(
						"No preview available for this print format"
					)}</div>`;

				idoc.open();
				idoc.write(
					`<!DOCTYPE html><html><head><meta charset="utf-8">` +
						`<style>${out.style || ""}</style>` +
						`<link rel="stylesheet" href="${base}${print_css}">` +
						`</head><body><div class="print-format print-format-preview">` +
						`${body_html}</div></body></html>`
				);
				idoc.close();

				// Grow the iframe to its content height (like Frappe's print view),
				// so short docs get a short preview and long docs scroll.
				const fit = () => {
					try {
						const el = idoc.body.querySelector(".print-format");
						if (el && el.offsetHeight) {
							iframe.style.height = el.offsetHeight + "px";
						}
					} catch (e) {
						// ignore
					}
				};
				fit();
				setTimeout(fit, 250);
			}

			d.$wrapper.on("hidden.bs.modal", () => {
				if (d._req) {
					try {
						d._req.abort();
					} catch (e) {
						// ignore
					}
				}
			});

			d.show();
			render_preview();
		});
}

function sign_and_print(frm, opts) {
	frappe.call({
		method: "pfx_sign.api.sign_document_pdf",
		args: {
			doctype: frm.doctype,
			docname: frm.doc.name,
			pfx_sign_setting: opts.pfx_sign_setting,
			password: opts.password,
			print_format_name: opts.print_format,
			letterhead: opts.letterhead,
			no_letterhead: opts.no_letterhead,
		},
		freeze: true,
		freeze_message: __("Signing document..."),
		callback: function (r) {
			if (!r.message || !r.message.token) {
				frappe.msgprint(__("Something went wrong"));
				return;
			}

			// Served from the server so the tab title and the download name
			// are the real file name (a blob: URL would show a UUID).
			window.open(
				"/api/method/pfx_sign.api.get_signed_pdf?token=" +
					encodeURIComponent(r.message.token),
				"_blank"
			);

			frappe.show_alert({
				message: __("Signed PDF ready — opening for print."),
				indicator: "green",
			});
		},
	});
}

function setup_sign_button(frm) {
	if (!frm || !frm.doc) {
		return;
	}

	// Only show for submitted documents.
	if (frm.doc.docstatus !== 1) {
		return;
	}

	// Prevent duplicate buttons on repeated refresh/form-load events.
	if (frm.custom_buttons && frm.custom_buttons[SIGN_BUTTON_LABEL]) {
		return;
	}

	frm.add_custom_button(SIGN_BUTTON_LABEL, function () {
		open_sign_dialog(frm);
	})
		.addClass("btn-primary")
		.prepend('<i class="fa fa-file-signature"></i> ');
}

$(document).on("form-refresh", function (event, frm) {
	if (!frm || !frm.doctype || !frm.doc) {
		return;
	}

	pfx_sign.get_eligible_settings(frm.doctype).then((eligible) => {
		if (Array.isArray(eligible) && eligible.length) {
			setup_sign_button(frm);
		}
	});
});
