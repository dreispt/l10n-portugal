# Copyright (C) 2021 Open Source Integrators
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from markupsafe import Markup

from odoo import api, exceptions, fields, models


class AccountMove(models.Model):
    _inherit = "account.move"

    @api.depends("move_type", "journal_id", "partner_shipping_id")
    def _compute_invoicexpress_doc_type(self):
        """
        The type of document to create: invoices, invoice_receipts,
        simplified_invoices, vat_moss_invoices, credit_notes or debit_notes.
        """
        invoices = self.filtered("journal_id.use_invoice_api")
        for invoice in invoices:
            doctype = invoice.journal_id.invoicexpress_doc_type
            if not doctype or doctype == "none":
                res = None
            elif invoice.move_type == "out_refund":
                res = "credit_note"
            else:
                res = doctype
            invoice.invoicexpress_doc_type = res

    @api.depends("can_invoice_api", "company_id.invoicexpress_template_id")
    def _compute_can_invoice_api_email(self):
        for invoice in self:
            invoice.can_invoice_api_email = (
                invoice.can_invoice_api
                and invoice.company_id.invoicexpress_template_id
            )

    invoicexpress_doc_type = fields.Selection(
        [
            ("invoice", "Invoice"),
            ("invoice_receipt", "Invoice and Receipt"),
            ("simplified_invoice", "Simplified Invoice"),
            ("vat_moss_invoice", "Europe VAT MOSS Invoice"),
            ("vat_moss_credit_note", "Europe VAT MOSS Credit Note"),
            ("debit_note", "Debit Note"),
            ("credit_note", "Credit Note"),
        ],
        compute="_compute_invoicexpress_doc_type",
        store=True,
        readonly=False,
        copy=False,
        help="Select the type of legal invoice document"
        " to be created by InvoiceXpress."
        " If unset, InvoiceXpress will not be used.",
    )

    @api.constrains("journal_id", "company_id")
    def _check_invoicexpress_doctype_config(self):
        """
        Ensure Journal configuration was not forgotten.
        """
        sale_invoices = self.filtered(lambda x: x.journal_id.type == "sale")
        for invoice in sale_invoices:
            journal_doctype = invoice.journal_id.invoicexpress_doc_type
            has_invoicexpress = invoice.company_id.has_invoicexpress
            if not journal_doctype and has_invoicexpress:
                raise exceptions.UserError(
                    self.env._(
                        "Journal %(journal)s is missing the InvoiceXpress"
                        " document type configuration!",
                        journal=invoice.journal_id.display_name,
                    )
                )

    def _get_invoice_api_partner(self):
        # Use InvoiceXpress contact sync for the customer
        return self.commercial_partner_id

    def _prepare_invoice_api_partner_vals(self, partner):
        res = partner.set_invoicexpress_contact()
        vals = super()._prepare_invoice_api_partner_vals(partner)
        vals["external_ref"] = res.get("code") or vals.get("reference") or ""
        vals["language"] = partner._prepare_invoicexpress_vals().get("language", "")
        return vals

    def _prepare_invoice_api_line_vals(self, line):
        self.ensure_one()
        tax = line.tax_ids[:1]
        # Ensure Taxes are created on InvoiceXpress
        tax.action_invoicexpress_tax_create()
        # Because InvoiceXpress expects unit_price in EUR,
        # check if we need to convert line currency to company currency
        if line.currency_id == line.company_id.currency_id:
            price_unit = line.price_unit
        else:
            price_unit = line.currency_id._convert(
                line.price_unit,
                line.company_id.currency_id,
                line.company_id,
                line.move_id.invoice_date
                or line.move_id.date
                or fields.Date.context_today(line),
            )
        return {
            "name": line.product_id.default_code
            or line.product_id.display_name
            or "",
            "description": line._get_invoicexpress_descr(),
            "quantity": line.quantity,
            "unit_price": price_unit,
            "discount": line.discount,
            "unit_of_measure": line.product_uom_id.name if line.product_uom_id else "",
            "tax": {
                "name": tax.name or "IVA0",
                "amount": tax.amount or 0.0,
                "exemption_reason": "",
            },
        }

    def _prepare_invoice_api_vals(self):
        vals = super()._prepare_invoice_api_vals()
        vals["document_type"] = self.invoicexpress_doc_type or "invoice"
        exempt_code = self.l10npt_vat_exempt_reason.code
        if exempt_code:
            vals["tax_exemption"] = exempt_code
        if self.company_id.currency_id != self.currency_id:
            currency_rate = self.env["res.currency"]._get_conversion_rate(
                self.company_id.currency_id,
                self.currency_id,
                self.company_id,
                self.invoice_date,
            )
            vals["exchange_rate"] = currency_rate
        doctype = vals["document_type"]
        if doctype in ("credit_note", "debit_note"):
            owner_invoice_num = self.reversed_entry_id.external_invoice_id
            if owner_invoice_num:
                vals["owner_invoice_id"] = owner_invoice_num
        return vals

    def _set_external_invoice_status(self, result):
        super()._set_external_invoice_status(result)
        if self.external_invoice_number:
            if self.payment_reference == self.name:
                self.payment_reference = self.external_invoice_number
            self.name = self.external_invoice_number

    def _prepare_invoice_api_email_vals(self):
        self.ensure_one()
        template_id = self.company_id.invoicexpress_template_id
        if not template_id:
            return None
        values = template_id._generate_template(
            [self.id], ["subject", "body_html", "email_to", "email_cc"]
        )[self.id]
        if not values.get("email_to"):
            return None
        return {
            "doctype": self.invoicexpress_doc_type,
            "message": {
                "client": {"email": values["email_to"], "save": "0"},
                "cc": values["email_cc"],
                "subject": values["subject"],
                "body": values["body_html"],
            },
        }

    def _post(self, soft=True):
        for invoice in self.filtered("can_invoice_api"):
            invoice._check_invoicexpress_doctype_config()
        res = super()._post(soft=soft)
        for invoice in self:
            if invoice.can_invoice_api and invoice.external_invoice_id:
                invoice.action_send_external_invoice_email()
        return res

    def _track_subtype(self, init_values):
        res = super()._track_subtype(init_values)
        if "payment_state" in init_values and self.payment_state == "paid":
            for invoice in self:
                if invoice.external_invoice_id:
                    invoice._mark_invoice_paid()
        return res

    def _mark_invoice_paid(self):
        for invoice in self.filtered("can_invoice_api"):
            doctype = invoice.invoicexpress_doc_type
            if not doctype:
                raise exceptions.UserError(
                    self.env._("Invoice is missing the InvoiceXpress document type!")
                )
            backend = invoice._get_invoice_api_backend()
            client = backend.get_client()
            client.mark_paid(invoice.external_invoice_id, doctype)
            msg = self.env._("InvoiceXpress record has been modified to Paid.")
            self.message_post(body=Markup(msg))


class AccountMoveLine(models.Model):
    _inherit = "account.move.line"

    def _get_invoicexpress_descr(self):
        """
        Remove Odoo product code from description,
        since it is already presented in a the Code column
        """
        res = self.name
        ref = self.product_id.default_code
        prefix = f"[{ref}] "
        if ref and self.name.startswith(prefix):
            res = self.name[len(prefix) :]
        return res
