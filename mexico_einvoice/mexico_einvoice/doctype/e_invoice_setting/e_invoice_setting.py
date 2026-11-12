# Copyright (c) 2023, Beveren-Software-Inc and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class EInvoiceSetting(Document):
    def validate(self):
        seen = set()
        for row in self.company_keys or []:
            if row.company in seen:
                frappe.throw(_("Company {0} is listed more than once in Company Keys").format(row.company))
            seen.add(row.company)

        if not self.secret_key and not self.company_keys:
            frappe.throw(_("Set a Default Secret Key or add at least one row in Company Keys"))
