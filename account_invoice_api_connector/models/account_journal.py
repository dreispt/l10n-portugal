# Copyright (C) 2021 Open Source Integrators
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import api, fields, models


class AccountJournal(models.Model):
    _inherit = "account.journal"

    @api.depends("invoice_api_backend_id", "invoice_api_backend_id.state")
    def _compute_use_invoice_api(self):
        for journal in self:
            journal.use_invoice_api = (
                journal.invoice_api_backend_id
                and journal.invoice_api_backend_id.state == "enabled"
            )

    invoice_api_backend_id = fields.Many2one(
        "invoice.api.backend",
        string="Invoice API Backend",
        check_company=True,
    )
    use_invoice_api = fields.Boolean(
        compute="_compute_use_invoice_api",
        help="External invoice API service is enabled for this journal.",
    )
