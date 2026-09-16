"""``POST /v4/Template`` — the delivery and PDF templates on your account."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ._base import AsyncResource, Operation, Resource


@dataclass(frozen=True)
class Template:
    """A delivery template — the email or SMS your recipients receive."""

    #: Pass this as ``template_id`` when ordering.
    id: int
    name: str | None
    #: Template type, e.g. email or SMS, as named by the API.
    type: str | None
    #: ISO alpha-2 language code.
    language: str | None
    sender: str | None
    subject: str | None
    #: Template body including HTML.
    formatted_text: str | None
    #: Template body as plain text.
    plain_text: str | None


@dataclass(frozen=True)
class PdfTemplate:
    """A PDF template — delivers the codes as a document attached to an email."""

    #: Pass this as ``pdf_template_uid`` when ordering, together with the
    #: ``template_id`` of an email template.
    uid: str | None
    name: str | None
    #: PDF template type, as named by the API.
    type: str | None
    #: ISO alpha-2 language code.
    language: str | None
    #: The country the template can be used for; ``None`` if any country.
    country: str | None
    #: The brand the template can be used for; ``None`` if any brand.
    brand_name: str | None


@dataclass(frozen=True)
class ListTemplatesResult:
    templates: list[Template] = field(default_factory=list)
    #: PDF templates, used to deliver the codes as a document attached to an email.
    pdf_templates: list[PdfTemplate] = field(default_factory=list)


def _operation() -> Operation:
    # body=None means no request body at all. The endpoint declares no
    # requestBody in the specification, so the SDK sends none — live-confirmed
    # as accepted.
    return Operation(method="POST", path="/v4/Template", body=None, retryable=True)


def _map(data: Any) -> ListTemplatesResult:
    payload = data or {}
    return ListTemplatesResult(
        templates=[
            Template(
                id=row.get("Id") or 0,
                name=row.get("Name"),
                type=row.get("Type"),
                language=row.get("Language"),
                sender=row.get("Sender"),
                subject=row.get("Subject"),
                formatted_text=row.get("FormattedText"),
                plain_text=row.get("PlainText"),
            )
            for row in payload.get("Templates") or []
        ],
        pdf_templates=[
            PdfTemplate(
                uid=row.get("Uid"),
                name=row.get("Name"),
                type=row.get("Type"),
                language=row.get("Language"),
                country=row.get("Country"),
                brand_name=row.get("BrandName"),
            )
            for row in payload.get("PDFTemplates") or []
        ],
    )


class TemplatesResource(Resource):
    def list(self) -> ListTemplatesResult:
        """List the delivery templates available to your account.

        ``POST /v4/Template``

        Returns both the email/SMS templates (``templates``) and the PDF
        templates (``pdf_templates``) that attach the codes to an email as a
        document.

        The endpoint declares no request body in the API specification, so this
        client sends none — confirmed accepted by the live API.

        Note: when the account has **no active templates**, the API answers
        ``404`` ("There were no active templates") rather than an empty list, so
        this method raises :class:`~huuray.HuurayNotFoundError` in that case —
        catch it and treat it as "no templates exist".
        """
        return _map(self._client._send(_operation()).data)


class AsyncTemplatesResource(AsyncResource):
    async def list(self) -> ListTemplatesResult:
        """List the delivery templates available to your account.

        ``POST /v4/Template``
        """
        return _map((await self._client._send(_operation())).data)
