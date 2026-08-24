# Copyright (C) 2021 Open Source Integrators
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import fields, models


class InvoiceApiBackend(models.Model):
    _name = "invoice.api.backend"
    _description = "External Invoice API Backend"
    _check_company_auto = True

    name = fields.Char(required=True)
    provider = fields.Selection([], string="Provider", required=True)
    company_id = fields.Many2one(
        "res.company",
        string="Company",
        required=True,
        default=lambda self: self.env.company,
    )
    api_url = fields.Char(string="API Endpoint Base URL")
    api_key = fields.Char(string="API Key", password=True)
    state = fields.Selection(
        [("draft", "Draft"), ("enabled", "Enabled")],
        default="draft",
    )
    active = fields.Boolean(default=True)

    def get_client(self):
        """Factory method to return a concrete API client instance."""
        raise NotImplementedError("Subclasses must implement get_client()")
