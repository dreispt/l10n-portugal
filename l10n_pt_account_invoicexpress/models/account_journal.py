# Copyright (C) 2021 Open Source Integrators
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import api, fields, models


class AccountJournal(models.Model):
    _inherit = "account.journal"

    @api.depends(
        "invoicexpress_doc_type",
        "invoice_api_backend_id",
        "invoice_api_backend_id.state",
    )
    def _compute_use_invoice_api(self):
        for journal in self:
            journal.use_invoice_api = (
                journal.invoicexpress_doc_type
                and journal.invoicexpress_doc_type != "none"
                and journal.invoice_api_backend_id
                and journal.invoice_api_backend_id.state == "enabled"
            )

    invoicexpress_doc_type = fields.Selection(
        [
            ("invoice", "Invoice"),
            ("invoice_receipt", "Invoices Receipt"),
            ("simplified_invoice", "Simplified Invoice"),
            ("none", "No InvoiceXpress document"),
        ],
        help="Select the type of legal invoice document"
        " to be created by InvoiceXpress.",
    )
