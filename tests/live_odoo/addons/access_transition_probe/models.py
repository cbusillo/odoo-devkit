from odoo import fields, models


class AccessTransitionProbe(models.Model):
    _name = "access.transition.probe"
    _description = "Synthetic native access transition oracle"

    name = fields.Char(required=True)
    user_id = fields.Many2one("res.users", required=True)
    company_id = fields.Many2one("res.company", required=True)
    marker = fields.Char()
