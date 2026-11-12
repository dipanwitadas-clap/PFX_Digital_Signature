import frappe
import hmac
import json
import os
from io import BytesIO

from frappe import _
from frappe.utils import cint

from frappe.utils.data import get_system_timezone, get_datetime_in_timezone

from pyhanko.sign import signers
from pyhanko.sign.signers import PdfSigner, PdfSignatureMetadata
from pyhanko.sign.fields import SigFieldSpec, append_signature_field
from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
from pyhanko.stamp import TextStampStyle
from pyhanko.pdf_utils.images import PdfImage

from PyPDF2 import PdfReader, PdfWriter


@frappe.whitelist()
def get_allowed_doctypes(customer):
    """
    Return the list of DocTypes configured under this customer's
    Pfx Sign Setting -> Allow Doctype.
    """
    if not customer or not frappe.db.exists("Pfx Sign Setting", customer):
        return []

    digi = frappe.get_doc("Pfx Sign Setting", customer)
    allowed = []

    for row in digi.allow_doctype or []:
        doctype = row.get("doc_type")
        if doctype:
            allowed.append(doctype)

    return allowed


def user_has_dsc_permission(user=None):
    """
    True if the User's "Permission for PFX Sign" custom check is ticked.
    Administrator and System Manager always bypass this check.
    """
    user = user or frappe.session.user
    if user == "Administrator" or "System Manager" in frappe.get_roles(user):
        return True
    return bool(frappe.db.get_value("User", user, "permission_for_pfx_sign"))


@frappe.whitelist()
def get_eligible_settings(doctype):
    """
    Return the names of all Pfx Sign Settings whose Allow Doctype
    table includes `doctype`, independent of any document's Customer.
    Returns nothing for users without "Permission for PFX Sign", so the
    sign button is hidden for them.
    """
    if not doctype or not user_has_dsc_permission():
        return []

    parents = frappe.get_all(
        "Pfx Sign Allowed Doctype",
        filters={"doc_type": doctype, "parenttype": "Pfx Sign Setting"},
        pluck="parent",
    )
    valid_settings = set(frappe.get_all("Pfx Sign Setting", pluck="name"))

    return [name for name in dict.fromkeys(parents) if name in valid_settings]


def log_sign_event(
    reference_doctype,
    reference_name,
    status,
    pfx_sign_setting=None,
    pfx_file_used=0,
    print_format=None,
    letter_head=None,
    signed_file_name=None,
    error_message=None,
    commit=False,
):
    """
    Record who signed which document (Pfx Sign Log). Never blocks
    signing: any failure here is logged to the Error Log and swallowed.
    `commit` is needed on failure paths, since frappe.throw rolls back.
    """
    try:
        log = frappe.get_doc(
            {
                "doctype": "Pfx Sign Log",
                "signed_by": frappe.session.user,
                "signed_on": frappe.utils.now_datetime(),
                "status": status,
                "reference_doctype": reference_doctype,
                "reference_name": reference_name,
                "pfx_sign_setting": pfx_sign_setting,
                "pfx_file_used": cint(pfx_file_used),
                "print_format": print_format,
                "letter_head": letter_head,
                "signed_file_name": signed_file_name,
                "error_message": error_message,
            }
        ).insert(ignore_permissions=True)
        if commit:
            frappe.db.commit()
        return log.name
    except Exception:
        frappe.log_error(title="Pfx Sign Log failed")


@frappe.whitelist()
def sign_document_pdf(
    doctype,
    docname,
    pfx_sign_setting,
    password,
    print_format_name=None,
    letterhead=None,
    no_letterhead=None
):
    """
    Generic PDF signing for ANY DocType, using the signing
    certificate configured on the manually selected Pfx Sign
    Setting, gated by the signer typing that setting's password.
    """

    # =====================================================
    # 1. Load Document & Pfx Sign Setting
    # =====================================================

    if not user_has_dsc_permission():
        log_sign_event(
            doctype, docname, "Failed",
            pfx_sign_setting=pfx_sign_setting,
            error_message="User does not have Permission for PFX Sign.",
            commit=True,
        )
        frappe.throw(_("You do not have Permission for PFX Sign."), frappe.PermissionError)

    doc = frappe.get_doc(doctype, docname)

    if not pfx_sign_setting or not frappe.db.exists("Pfx Sign Setting", pfx_sign_setting):
        frappe.throw(_("Invalid Pfx Sign Setting selected."))

    digi = frappe.get_doc("Pfx Sign Setting", pfx_sign_setting)

    # =====================================================
    # 2. Security Validation (Allow Doctype)
    # =====================================================
    # Ensure the requested DocType is present in the selected
    # Pfx Sign Setting's "Allow Doctype" configuration so direct
    # API calls for unauthorized DocTypes are rejected.

    allowed_doctypes = [row.get("doc_type") for row in (digi.allow_doctype or []) if row.get("doc_type")]

    if doctype not in allowed_doctypes:
        log_sign_event(
            doctype, docname, "Failed",
            pfx_sign_setting=pfx_sign_setting,
            pfx_file_used=1,
            error_message="DocType not allowed for this setting.",
            commit=True,
        )
        frappe.throw(
            _("Digital signing is not allowed for {0} under the selected Pfx Sign Setting.").format(doctype)
        )

    # =====================================================
    # 3. Password Verification
    # =====================================================

    actual_password = digi.get_password("dsc_password")

    if not password or not hmac.compare_digest(str(password), str(actual_password or "")):
        log_sign_event(
            doctype, docname, "Failed",
            pfx_sign_setting=pfx_sign_setting,
            pfx_file_used=1,
            error_message="Incorrect password.",
            commit=True,
        )
        frappe.throw(_("Incorrect password."), title=_("Incorrect Password"))

    # =====================================================
    # 4. Generate PDF
    # =====================================================

    # If a print format was requested, make sure it actually belongs to
    # this DocType so arbitrary format names can't be forced via the API.
    if print_format_name and print_format_name != "Standard":
        pf_doctype = frappe.db.get_value(
            "Print Format", print_format_name, "doc_type"
        )
        if pf_doctype and pf_doctype != doctype:
            frappe.throw(
                f"Print format {print_format_name} does not belong to {doctype}."
            )

    print_format = (
        print_format_name
        or frappe.get_meta(doctype).default_print_format
        or "Standard"
    )

    # Whitelisted args arrive as strings; normalize the letterhead choice.
    if no_letterhead is not None:
        no_letterhead = cint(no_letterhead)
    else:
        no_letterhead = 0 if letterhead else 1

    if not letterhead:
        no_letterhead = 1

    # When "Repeat Header and Footer in PDF" is enabled (Print Settings), Frappe
    # renders the letter head into wkhtmltopdf's --header-html. Unpatched
    # wkhtmltopdf silently ignores that switch, so the letter head disappears
    # from the signed PDF. Force it off for this render so the letter head is
    # drawn inline in the document body and always shows up.
    prev_settings = frappe.local.form_dict.get("settings")
    if letterhead and not no_letterhead:
        frappe.local.form_dict.settings = json.dumps({"repeat_header_footer": 0})

    try:
        pdf_content = frappe.get_print(
            doctype,
            docname,
            print_format=print_format,
            as_pdf=True,
            letterhead=letterhead or None,
            no_letterhead=no_letterhead
        )
    finally:
        if prev_settings is None:
            frappe.local.form_dict.pop("settings", None)
        else:
            frappe.local.form_dict.settings = prev_settings

    # =====================================================
    # 5. Signature Image
    # =====================================================

    signature_image_path = None

    if digi.signature_image:

        signature_image_path = frappe.get_site_path(
            digi.signature_image.lstrip("/")
        )

        if not os.path.exists(signature_image_path):
            signature_image_path = None

    # =====================================================
    # 6. Load DSC Signer
    # =====================================================

    if not digi.pfx_file:
        frappe.throw(_("PFX file is missing in the selected Pfx Sign Setting."))

    pfx_path = frappe.get_site_path(
        digi.pfx_file.lstrip("/")
    )

    if not os.path.exists(pfx_path):
        frappe.throw(_("PFX file not found"))

    signer = signers.SimpleSigner.load_pkcs12(
        pfx_path,
        passphrase=actual_password.encode()
    )

    # =====================================================
    # 7. Prepare PDF
    # =====================================================

    # Set the PDF Title metadata so the browser's PDF viewer shows the
    # signed file name instead of a blob UUID. Must happen before signing.
    reader = PdfReader(BytesIO(pdf_content))
    titled = PdfWriter()
    titled.clone_document_from_reader(reader)
    titled.add_metadata({"/Title": f"{docname}-signed"})
    titled_io = BytesIO()
    titled.write(titled_io)

    input_pdf = BytesIO(titled_io.getvalue())

    num_pages = len(PdfReader(input_pdf).pages)

    input_pdf.seek(0)

    signed_pdf_io = input_pdf

    # =====================================================
    # 8. Signature Position
    # =====================================================
    # (left, bottom, right, top)

    box = (345, 45, 545, 125)

    # =====================================================
    # 9. Signature Style
    # =====================================================

    tz = get_system_timezone()
    now = get_datetime_in_timezone(tz)

    offset = now.strftime("%z")
    offset_fmt = offset[:3] + "'" + offset[3:5] + "'"
    signed_date = now.strftime("%Y.%m.%d %H:%M:%S") + " " + offset_fmt

    stamp_style = TextStampStyle(

        border_width=1,

        stamp_text=(
            "Digitally Signed By:\n"
            "%(signer)s\n"
            "Date: %(signed_date)s"
        ),

        background=PdfImage(signature_image_path)
        if signature_image_path else None
    )

    # =====================================================
    # 10. Multiple Page Signing
    # =====================================================

    if int(digi.multiple_page_sign or 0) == 1:

        for i in range(num_pages):

            signed_pdf_io.seek(0)

            writer = IncrementalPdfFileWriter(
                signed_pdf_io
            )

            output = BytesIO()

            field_name = f"Signature_Page_{i + 1}"

            append_signature_field(
                writer,
                SigFieldSpec(
                    sig_field_name=field_name,
                    box=box,
                    on_page=i
                )
            )

            pdf_signer = PdfSigner(

                PdfSignatureMetadata(
                    field_name=field_name,
                    reason=f"Digitally signed on {doctype}",
                    location=digi.sign_address or "India"
                ),

                signer=signer,

                stamp_style=stamp_style
            )

            pdf_signer.sign_pdf(
                writer,
                output=output,
                appearance_text_params={
                    "signed_date": signed_date,
                }
            )

            signed_pdf_io = output

    # =====================================================
    # 11. Last Page Signing
    # =====================================================

    else:

        writer = IncrementalPdfFileWriter(
            input_pdf
        )

        output = BytesIO()

        append_signature_field(
            writer,
            SigFieldSpec(
                sig_field_name="Signature_Last_Page",
                box=box,
                on_page=num_pages - 1
            )
        )

        pdf_signer = PdfSigner(

            PdfSignatureMetadata(
                field_name="Signature_Last_Page",
                reason=f"Digitally signed on {doctype}",
                location=digi.sign_address or "India"
            ),

            signer=signer,

            stamp_style=stamp_style
        )

        pdf_signer.sign_pdf(
            writer,
            output=output,
            appearance_text_params={
                "signed_date": signed_date,
            }
        )

        signed_pdf_io = output

    # =====================================================
    # 12. Return Response
    # =====================================================
    # The signed PDF is kept only in a short-lived cache (not saved as a File
    # attachment) and served by get_signed_pdf with a proper filename, so
    # nothing shows up under the document's Attachments.

    file_name = f"{docname}-signed.pdf"
    token = frappe.generate_hash(length=32)
    frappe.cache().set_value(
        f"pfx_sign_pdf:{token}",
        {
            "user": frappe.session.user,
            "file_name": file_name,
            "content": signed_pdf_io.getvalue(),
        },
        expires_in_sec=300,
    )

    log_sign_event(
        doctype, docname, "Success",
        pfx_sign_setting=pfx_sign_setting,
        pfx_file_used=1,
        print_format=print_format_name,
        letter_head=letterhead,
        signed_file_name=f"{docname}-signed.pdf",
    )

    return {
        "status": "success",
        "file_name": file_name,
        "token": token,
    }


@frappe.whitelist()
def get_signed_pdf(token):
    """
    Serve a just-signed PDF from the short-lived cache with a real
    filename, so the browser shows/suggests it instead of a blob UUID.
    """
    data = frappe.cache().get_value(f"pfx_sign_pdf:{token}")

    if not data:
        frappe.throw(_("This signed PDF link has expired. Please sign again."))

    if data.get("user") != frappe.session.user:
        frappe.throw(_("Not permitted."), frappe.PermissionError)

    frappe.local.response.filename = data["file_name"]
    frappe.local.response.filecontent = data["content"]
    frappe.local.response.type = "pdf"