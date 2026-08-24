# Copyright (C) 2021 Open Source Integrators
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import fields, models


class InvoiceApiBackend(models.Model):
    _inherit = "invoice.api.backend"

    provider = fields.Selection(
        selection_add=[("invoicexpress", "InvoiceXpress")],
        ondelete={"invoicexpress": "cascade"},
    )
    invoicexpress_account_name = fields.Char(string="Account Name")

    def get_client(self):
        if self.provider == "invoicexpress":
            return self.env["invoice.api.adapter.invoicexpress"].with_context(
                invoice_api_backend_id=self.id
            )
        return super().get_client()
