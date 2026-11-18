"""Contextual review guidance applied to baseline compliance rules during schema setup."""

CONTEXTUAL_BASELINE = {
    "Rule 8": (
        "Check whether defense-agency, active-bid or product-specification claims require documented Comms/IR "
        "and Legal clearance. NEEDS_REVIEW: relevant claims appear without evidence of the required sign-off. "
        "Do not infer that clearance is missing merely because it is not stated in public copy."
    ),
    "Rule 18": (
        "Check factual claims against dated sources, specifications, filings, test records and authorizations. "
        "EVIDENCE_REQUIRED: a material factual claim lacks supporting evidence. NO_VIOLATION: the statement "
        "is clearly opinion or its evidence supports the same claim, scope and date."
    ),
    "Rule 20": (
        "Check whether image editing changes who attended, equipment, events, results, configuration or relationships. "
        "VIOLATION: available originals or edit records demonstrate a misleading change. NEEDS_REVIEW: an edit "
        "may alter those facts and its provenance is unclear. Ordinary correction or security blurring is allowed "
        "when the result remains accurate; do not assume an image was manipulated without evidence."
    ),
    "Rule 21": (
        "Check image and video provenance, commercial rights, releases and required credits. VIOLATION: the "
        "material is known to be unlicensed or unauthorized. NEEDS_REVIEW: rights or provenance cannot be "
        "established from the supplied evidence. Public availability alone does not establish commercial rights; "
        "absence of rights information in a caption does not prove that permission is missing."
    ),
    "Rule 22": (
        "Apply the same disclosure and substantiation rules to replies, reposts, comments, descriptions and DMs. "
        "VIOLATION: a reply discloses unsupported sensitive claims or paid/employee promotion omits required "
        "connection disclosure. NEEDS_REVIEW: the relationship or claim clearance is unclear. NO_VIOLATION: "
        "the reply makes no new claim and points to an approved public source."
    ),

    "Rule 29": (
        "Decide whether the passage shows or promotes death, killing, violence, terrorism, weapons or self-harm. "
        "VIOLATION: violent, threatening or graphic wording or imagery (a slogan like 'KILL' or 'DEATH' on a vehicle, "
        "clothing or sign; a weapon shown or named as part of the post; joking about killing, death or suicide). "
        "NEEDS_REVIEW: a defense or security post that names threats, weapons or terrorism factually (for example "
        "'detects threats before they reach the base'), since the wording has to be checked against the defense "
        "rulebook. NO_VIOLATION: an ordinary figure of speech or a technical term ('killer app', 'kill switch', "
        "'battery is dead', 'deadline'), or a news or safety context that doesn't glorify harm."
    ),
    "Rule 4": (
        "Decide whether the post actually uses cleared U.S. Department of Defense visual information, a service's seal, "
        "mark or personnel, or implies endorsement by a defense agency. VIOLATION: it says or shows endorsement, "
        "partnership or approval by a defense agency, or says the footage is official DoD/DVIDS material without the "
        "disclaimer. NEEDS_REVIEW: the passage only names a service or agency (e.g. 'a US Navy submarine in 2017') and "
        "the footage or its source isn't stated, so the visual has to be checked: if it is cleared DoD visual "
        "information the disclaimer is required. NO_VIOLATION: a plain factual or historical mention with no imagery or "
        "endorsement involved."
    ),
    "Rule 9": (
        "Decide whether the passage presents AI-generated, simulated or concept imagery as real, by what KIND of visual "
        "it is. NO_VIOLATION: an educational or explanatory animation or diagram (for example an animation of how a "
        "radio technique, a process or an idea works), a historical illustration, patent drawing or archive image, "
        "licensed stock, or the words 'concept', 'animation' or 'visualization' used in another sense (a business "
        "concept, an animated discussion). An explainer animation is not a fabricated photograph. NEEDS_REVIEW: a "
        "conceptual render or visualization of the company's product, a future use or a defense scenario (even one "
        "already described as 'clearly labeled'), because only the final video shows whether the 'Conceptual "
        "visualization' label is on screen; or an animation that could be mistaken for real footage of an operation, "
        "test or deployment. VIOLATION: AI-generated or simulated imagery, photorealistic scenes or synthetic military "
        "footage presented as real footage of a real operation, test, deployment or customer. Actual military footage is "
        "a different rule (4 and 13), not this one."
    ),
    "Rule 16": (
        "Decide who or what the sentence is about and whether it claims a CURRENT ZenaTech/ZenaDrone capability that is "
        "really future, planned or unproven. VIOLATION: it states a future or unproven capability of the company's "
        "product as present fact ('our drone solves...', 'guarantees', 'proves'). NO_VIOLATION: it refers to history "
        "or to another technology, person or company; it is clearly framed as the speaker's opinion or prediction "
        "('my bet', 'I think', 'in ten years'); or it is a hypothetical example not about the company's current "
        "capabilities. NEEDS_REVIEW: it is about the company's future capability and it is unclear whether it is "
        "framed as a goal or a fact."
    ),
    "Rule 19": (
        "Decide whether the passage depicts or discusses an ACTUAL ZenaTech/ZenaDrone test, demonstration or customer "
        "pilot, and whether it implies selection, adoption or validation by a customer or government. VIOLATION: it "
        "presents a real test, demo or pilot of the company's product as validation, selection or deployment ('the "
        "military puts ZenaDrone to the test', 'warfighters validate ZenaDrone', 'customer deploys ZenaDrone'), or "
        "publishes real test or pilot imagery or results as if released. NEEDS_REVIEW: it describes a real company "
        "test, demo or pilot and nothing says the organization, people, location and result are cleared for release. "
        "NO_VIOLATION: it names a military branch, agency or customer only as a historical, third-party or general "
        "reference (e.g. 'that happened on a US Navy submarine in 2017') with no ZenaTech test, demo or pilot "
        "involved and no claim of selection, adoption or validation. Naming an organization is not by itself a "
        "relationship, a test, an endorsement or a customer."
    ),
    "Rule 24": (
        "Decide whether the passage makes an absolute, superlative or unprovable PERFORMANCE claim about ZenaTech "
        "products or services. VIOLATION: it claims the best, perfect, zero-failure, 100%, guaranteed or unmatched "
        "performance of the company's product. NO_VIOLATION: it counts, compares or describes a scenario or "
        "operating model ('1 drone = 1 person', 'nobody flies just one drone'), or is clearly framed as an opinion or "
        "prediction. EVIDENCE_REQUIRED: it states a concrete, measurable result for the company's product "
        "('reduces counting time by 60%') without saying where it comes from."
    ),
}
