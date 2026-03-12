# Account Navigation: Working Multiple Contacts at One Company

## Companies vs Contacts

A company is an account. A contact is a person at that company. One company can have many contacts. Harvey must always create the company profile first, then attach contacts to it.

## Company Profiles

Every company Harvey researches must have:
- **Name**: The actual company name
- **Domain**: Their website domain (e.g., acme.com)
- **Website**: Full URL
- **Description**: What the company does (1-2 sentences)
- **Industry**: Primary industry
- **Company size**: Employee range
- **Location**: HQ city/region
- **Source**: How Harvey found them (google, linkedin, website scrape)
- **Source URL**: The exact URL where Harvey found the info

Harvey should pull this from the company's website, LinkedIn company page, or Google results. Do not guess. If you can't verify a field, leave it blank.

## Contact Profiles

Every contact must have at minimum:
- **First name**: Required. Must be real, verified.
- **Last name**: Required. Must be real, verified.
- **Title**: Required. Must be their actual current title.
- **Company**: Linked to the company profile.

Optional fields (only fill if verifiable):
- **Email**: Only include if verified via MX/SMTP check or found on the company website. Mark email_verified = true only if confirmed.
- **Phone**: Very difficult to find. Only include if listed publicly (company website, LinkedIn profile). Mark phone_verified = true only if confirmed. Do NOT guess phone numbers.
- **LinkedIn URL**: Include if found.
- **Seniority**: c_suite, vp, director, manager, individual. Infer from title.
- **Department**: Sales, marketing, engineering, operations, etc. Infer from title.

### Data Quality Rules
1. Never fabricate contact information. If you can't verify it, don't include it.
2. Email patterns (first.last@domain.com) are only valid after SMTP verification.
3. A contact without a verified email can still be valuable for LinkedIn outreach.
4. Always record where you found each piece of information (source + source_url).

## Who to Contact at a Company

### The Entry Point Matrix

Not all contacts are equal. Who you reach out to first depends on the deal type:

| Deal Type | First Contact | Second Contact | Avoid First |
|-----------|--------------|----------------|-------------|
| **Small purchase** (<$500/mo) | End user / manager | Their boss | C-suite (too senior) |
| **Mid-market** ($500-5k/mo) | Director / VP | Manager who'd use it | Individual contributors |
| **Enterprise** ($5k+/mo) | VP / C-suite | Champion (manager who'll advocate) | Junior staff |

### Multi-Threading: Working Multiple Contacts

When Harvey finds multiple contacts at one company:

1. **Start with one.** Never email multiple people at the same company simultaneously. It looks spammy and they'll compare notes.

2. **Start at the right level.** Pick the person most likely to:
   - Feel the pain your product solves
   - Have authority to evaluate solutions
   - Actually read and respond to email

3. **Move up or down based on response:**
   - No response from manager? Try the VP.
   - VP says "talk to my team"? Email the manager and reference the VP.
   - Manager is interested? Ask if they'd like to loop in their boss.

4. **Wait between contacts.** If you email Person A at Acme, wait at least 5 business days before emailing Person B at Acme. Treat the company as one account, not individual contacts.

5. **Share context across contacts.** If Person A replied, Harvey should know that when writing to Person B. "Your colleague Sarah mentioned she's evaluating options" is powerful.

### Seniority Detection

Infer seniority from the title:

- **C-Suite**: CEO, CTO, CFO, CMO, COO, CRO, "Chief __"
- **VP**: VP, Vice President, SVP, EVP, "Head of __"
- **Director**: Director, Senior Director, "Director of __"
- **Manager**: Manager, Senior Manager, Team Lead, "Manager of __"
- **Individual**: Analyst, Specialist, Coordinator, Associate, Engineer

### When to Stop

Stop contacting a company if:
- 3 different contacts have been emailed with no response
- Any contact explicitly says "not interested" or "stop emailing"
- The company doesn't match ICP after further research

## Sample Verification

Before adding prospects to the live pipeline, Harvey should create a sample batch of 5-10 company + contact profiles and present them to the user for review. This ensures:

1. The ICP targeting is correct (right industries, right company sizes)
2. The contact titles match what the user wants
3. The data quality meets expectations
4. The research sources are legitimate

### Sample Flow
1. Harvey researches 5-10 companies and their contacts
2. Presents them as a sample: "Here are 8 companies I found. Each has 1-2 contacts. Take a look and let me know if these are the right kind of targets."
3. User reviews and gives feedback ("too small", "wrong industry", "perfect", "we already work with them")
4. Harvey adjusts targeting based on feedback
5. After approval, Harvey proceeds with full prospecting

This sample step only happens once at the start (or when the ICP changes). After the user approves the sample, Harvey prospects autonomously.
