# Copyright (C) 2021 Open Source Integrators
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import models


class InvoiceApiAdapter(models.AbstractModel):
    _name = "invoice.api.adapter"
    _description = "Invoice API Adapter"

    def create_invoice(self, invoice_vals):
        """Send invoice values to external API to create and finalize a document.

        :param invoice_vals: dictionary with the standard invoice API payload.
        :return: dictionary with keys such as:
            - external_id: the external system identifier
            - number: the generated human readable number
            - url: a permalink to the document
            - state: the document state in the external system
        """
        raise NotImplementedError(
            self.env._("Subclasses must implement create_invoice()")
        )

    def cancel_invoice(self, external_id, reason=None):
        """Cancel an invoice in the external API."""
        raise NotImplementedError(
            self.env._("Subclasses must implement cancel_invoice()")
        )

    def send_email(self, external_id, email_data):
        """Trigger an email via external service."""
        raise NotImplementedError(
            self.env._("Subclasses must implement send_email()")
        )

    def get_document_pdf(self, external_id):
        """Fetch PDF binary content from external service."""
        raise NotImplementedError(
            self.env._("Subclasses must implement get_document_pdf()")
        )
