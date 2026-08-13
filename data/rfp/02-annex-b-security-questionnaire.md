# RFP-2026-014 — Annex B: Security Questionnaire

Suppliers must answer every question. Answers of "will be provided on request" are
scored 0.

## B.1 Certifications and attestations

| ID | Question | Response required |
|----|----------|-------------------|
| SEC-01 | Provide your current ISO/IEC 27001 certificate number, issuing body, and expiry date. | Certificate copy |
| SEC-02 | Do you hold SOC 2 Type II? Provide the report date and any qualified opinions. | Report summary |
| SEC-03 | Are you certified under an EU Cloud Code of Conduct or equivalent? | Yes/No + evidence |

## B.2 Data residency and sovereignty

| ID | Question |
|----|----------|
| SEC-10 | Name every region in which customer data is stored at rest. |
| SEC-11 | Name every region in which customer data is processed, including transient processing by AI models. |
| SEC-12 | Describe how you guarantee that AI inference does not route outside the EU. |
| SEC-13 | Does any subprocessor operate outside the EU? Provide the full subprocessor list. |

> Note: SEC-11 and SEC-12 are directly linked to mandatory requirement M-01.
> Suppliers using large language models must state the deployment type and the
> geography boundary of that deployment.

## B.3 Encryption

| ID | Question |
|----|----------|
| SEC-20 | State the encryption algorithm and key length used at rest. |
| SEC-21 | Do you support customer-managed keys? If so, describe the key rotation process. |
| SEC-22 | State the minimum TLS version accepted for data in transit. |

## B.4 Access control and identity

| ID | Question |
|----|----------|
| SEC-30 | Describe your administrative access model, including break-glass procedures. |
| SEC-31 | Do you support SAML 2.0 or OIDC federation with the authority's identity provider? |
| SEC-32 | Is multi-factor authentication enforced for all privileged accounts? |
| SEC-33 | State your maximum privileged-session duration. |

## B.5 AI-specific controls

| ID | Question |
|----|----------|
| SEC-40 | Describe content-filtering and abuse-monitoring controls applied to model input and output. |
| SEC-41 | Is customer data used to train, retrain, or fine-tune any model? |
| SEC-42 | Describe the human escalation path when the conversational agent cannot resolve an interaction. |
| SEC-43 | State the retention period for call audio, transcripts, and model prompts. |
| SEC-44 | Describe how the supplier detects and mitigates prompt injection in the conversational AI. |

## B.6 Incident response

| ID | Question |
|----|----------|
| SEC-50 | State your notification window for a confirmed personal data breach. |
| SEC-51 | Provide the last three years of reportable security incidents affecting the proposed service. |
| SEC-52 | Describe your penetration testing cadence and who performs it. |
