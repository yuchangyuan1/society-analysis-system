"""
Seed knowledge base with fact-check articles from authoritative public sources.
This script populates Chroma (vector store) and Kuzu (graph) so that
build_evidence_pack() can retrieve contradicting evidence and the analysis
chain can proceed past the INSUFFICIENT_EVIDENCE gate.

Run once before the first end-to-end test:
    python scripts/seed_knowledge.py

Run again (idempotent) any time to add more articles.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Ensure project root is on sys.path when run as a script
sys.path.insert(0, str(Path(__file__).parent.parent))

import structlog
from services.chroma_service import ChromaService
from services.embeddings_service import EmbeddingsService
from services.kuzu_service import KuzuService

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Seed articles
# Each entry contains factual content drawn from public-domain authoritative
# sources (WHO, CDC, IPCC, etc.).  The content fields are paraphrased
# summaries suitable for embedding; link to originals via `url`.
# ---------------------------------------------------------------------------
SEED_ARTICLES: list[dict] = [
    # ── 5G / COVID-19 ──────────────────────────────────────────────────────
    {
        "id": "who-5g-covid-debunk",
        "title": "5G mobile networks do NOT spread COVID-19",
        "url": "https://www.who.int/emergencies/diseases/novel-coronavirus-2019/advice-for-public/myth-busters",
        "source": "WHO",
        "content": (
            "5G mobile networks do not spread COVID-19. Viruses cannot travel on radio waves "
            "or mobile networks. COVID-19 is spreading in many countries that do not have 5G "
            "mobile networks. COVID-19 is spread through respiratory droplets when an infected "
            "person coughs, sneezes, or speaks. People can also be infected by touching a "
            "contaminated surface and then touching their eyes, mouth, or nose. Radio waves "
            "from 5G technology are non-ionizing and cannot damage DNA or cells."
        ),
    },
    {
        "id": "fcc-5g-safety",
        "title": "FCC: 5G radio frequency emissions are safe",
        "url": "https://www.fcc.gov/consumers/guides/understanding-wireless-phones-and-health-concerns",
        "source": "FCC",
        "content": (
            "The FCC's radio frequency (RF) exposure limits for 5G are set well below levels "
            "associated with any known harm to human health. 5G uses millimeter wave frequencies "
            "that are non-ionizing — they do not have enough energy to break chemical bonds or "
            "remove electrons. Independent scientific review bodies worldwide have found no "
            "credible evidence of health risks from 5G networks operating within regulatory limits."
        ),
    },
    # ── Vaccines / COVID-19 ────────────────────────────────────────────────
    {
        "id": "cdc-vaccines-do-not-cause-autism",
        "title": "Vaccines do not cause autism — CDC fact sheet",
        "url": "https://www.cdc.gov/vaccinesafety/concerns/autism.html",
        "source": "CDC",
        "content": (
            "Vaccines do not cause autism. Studies have repeatedly shown no link between "
            "childhood vaccines and autism spectrum disorder. The 1998 study that initially "
            "suggested a link between MMR vaccine and autism was later found to be seriously "
            "flawed, and the paper was fully retracted by The Lancet in 2010. The researcher "
            "lost his medical license due to ethical violations. Since then, multiple large-scale "
            "studies involving millions of children have found no association between vaccines "
            "and autism."
        ),
    },
    {
        "id": "who-covid-vaccine-safety",
        "title": "COVID-19 vaccines are safe and effective",
        "url": "https://www.who.int/news-room/feature-stories/detail/safety-of-covid-19-vaccines",
        "source": "WHO",
        "content": (
            "COVID-19 vaccines approved by WHO have been rigorously tested in clinical trials "
            "involving tens of thousands of participants and reviewed by independent experts. "
            "The vaccines do not alter human DNA. mRNA vaccines deliver instructions for cells "
            "to make the spike protein and are degraded by the body; mRNA never enters the cell "
            "nucleus. Post-authorization safety monitoring involving hundreds of millions of "
            "vaccinated people confirms the vaccines are safe, with serious adverse events "
            "being rare. Benefits of vaccination far outweigh the risks."
        ),
    },
    {
        "id": "nejm-mrna-dna",
        "title": "mRNA vaccines do not alter human DNA",
        "url": "https://www.nejm.org/doi/full/10.1056/NEJMoa2034577",
        "source": "NEJM",
        "content": (
            "mRNA vaccines work by delivering messenger RNA instructions to cells, which then "
            "produce a harmless piece of the virus's spike protein. The mRNA never enters the "
            "cell nucleus where DNA is stored and cannot integrate into DNA. After the "
            "instructions are used, the mRNA breaks down naturally within days. This mechanism "
            "is fundamentally different from DNA vaccines or gene therapy."
        ),
    },
    # ── Climate change ─────────────────────────────────────────────────────
    {
        "id": "ipcc-ar6-consensus",
        "title": "IPCC AR6: Human-caused climate change is unequivocal",
        "url": "https://www.ipcc.ch/report/ar6/wg1/",
        "source": "IPCC",
        "content": (
            "It is unequivocal that human influence has warmed the atmosphere, ocean and land. "
            "Widespread and rapid changes in the atmosphere, ocean, cryosphere and biosphere "
            "have occurred. Human-induced climate change is already affecting many weather and "
            "climate extremes in every region across the globe. Global surface temperature has "
            "increased faster since 1970 than in any other 50-year period over at least the "
            "last 2000 years. The IPCC report represents the consensus of thousands of "
            "scientists from 195 countries."
        ),
    },
    {
        "id": "nasa-climate-97-percent",
        "title": "97% of climate scientists agree on human-caused warming",
        "url": "https://climate.nasa.gov/scientific-consensus/",
        "source": "NASA",
        "content": (
            "Multiple studies published in peer-reviewed scientific journals show that 97% or "
            "more of actively publishing climate scientists agree that climate-warming trends "
            "over the past century are extremely likely due to human activities. In addition, "
            "most of the leading scientific organizations worldwide have issued public "
            "statements endorsing this position. The scientific consensus on climate change is "
            "one of the strongest in science."
        ),
    },
    # ── Election integrity ─────────────────────────────────────────────────
    {
        "id": "cisa-2020-election-secure",
        "title": "2020 US Election: Most secure in American history — CISA",
        "url": "https://www.cisa.gov/news-events/news/joint-statement-elections-infrastructure-government-coordinating-council-election",
        "source": "CISA",
        "content": (
            "The November 3rd election was the most secure in American history. There is no "
            "evidence that any voting system deleted or lost votes, changed votes, or was in "
            "any way compromised. This statement was issued by the Election Infrastructure "
            "Information Sharing and Analysis Center (EI-ISAC) Executive Committee and the "
            "Cybersecurity and Infrastructure Security Agency (CISA), representing officials "
            "from state and local governments as well as federal agencies."
        ),
    },
    {
        "id": "apnews-election-fraud-claims",
        "title": "AP: Courts, officials repeatedly rejected 2020 election fraud claims",
        "url": "https://apnews.com/article/voter-fraud-election-2020-joe-biden-donald-trump-7fcb6a134f47b280f5803b9b45dae59c",
        "source": "AP News",
        "content": (
            "More than 60 lawsuits challenging the 2020 election results were rejected by "
            "courts, including by judges appointed by both Republican and Democratic "
            "presidents. State election officials, election security experts, the Department "
            "of Justice, and the Supreme Court all found no evidence of widespread fraud "
            "sufficient to have changed the outcome of the election."
        ),
    },
    # ── 5G health / radiation (expanded) ──────────────────────────────────
    {
        "id": "who-nonionizing-radiation",
        "title": "WHO: Non-ionizing radiation from 5G does not damage cells or DNA",
        "url": "https://www.who.int/news-room/questions-and-answers/item/radiation-5g-networks",
        "source": "WHO",
        "content": (
            "5G uses non-ionizing electromagnetic radiation — the same category as FM radio, "
            "Wi-Fi, and visible light. Non-ionizing radiation does not have enough energy to "
            "break chemical bonds or remove electrons from atoms, so it cannot damage DNA or "
            "cellular structures at the exposure levels produced by 5G base stations. WHO's "
            "International EMF Project continuously reviews scientific literature and has found "
            "no confirmed health risks from low-level radiofrequency fields. The claim that 5G "
            "weakens the immune system or activates dormant viruses has no scientific basis."
        ),
    },
    {
        "id": "icnirp-5g-guidelines",
        "title": "ICNIRP: International 5G safety guidelines protect against all established health effects",
        "url": "https://www.icnirp.org/en/activities/news/news-article/5g-guidelines-2020.html",
        "source": "ICNIRP",
        "content": (
            "The International Commission on Non-Ionizing Radiation Protection (ICNIRP) 2020 "
            "guidelines confirm that 5G frequencies — including millimeter waves — are safe at "
            "the regulated exposure levels. The guidelines were developed after a thorough review "
            "of the entire body of science on radiofrequency electromagnetic fields. "
            "Radio waves from 5G cannot cause, spread, or activate viruses. Viruses are "
            "biological organisms that require direct physical contact or respiratory transmission; "
            "they are entirely unaffected by electromagnetic fields at these frequencies and "
            "power levels."
        ),
    },
    {
        "id": "fullFact-5g-covid-no-link",
        "title": "Full Fact: No evidence 5G causes or spreads COVID-19",
        "url": "https://fullfact.org/health/5g-and-coronavirus/",
        "source": "Full Fact",
        "content": (
            "There is no credible evidence that 5G causes or spreads COVID-19. The disease is "
            "caused by the SARS-CoV-2 virus, which spreads person to person via respiratory "
            "droplets and aerosols — not through radio waves. As of 2020, COVID-19 was actively "
            "spreading in many countries including Iran, Japan, and rural areas of Africa that "
            "had no 5G infrastructure at all, which conclusively rules out any causal link. "
            "The conspiracy theory has been debunked by WHO, PHE, and every major health body."
        ),
    },
    {
        "id": "covid19-transmission-mechanism",
        "title": "CDC: How COVID-19 spreads — respiratory droplets, not radio waves",
        "url": "https://www.cdc.gov/coronavirus/2019-ncov/transmission/index.html",
        "source": "CDC",
        "content": (
            "COVID-19 is caused by infection with SARS-CoV-2, a coronavirus. It spreads mainly "
            "through respiratory droplets and aerosols produced when an infected person breathes, "
            "talks, coughs, or sneezes. The virus can also spread by touching surfaces "
            "contaminated with the virus and then touching the mouth, nose, or eyes. "
            "Radio frequency radiation, including 5G millimeter waves, is physically incapable "
            "of carrying, creating, or activating viruses. There is no biological or physical "
            "mechanism by which any cellular network could influence viral infection."
        ),
    },
    {
        "id": "reuters-5g-immune-system",
        "title": "Reuters: No evidence 5G suppresses immune system or activates COVID-19",
        "url": "https://www.reuters.com/article/uk-factcheck-5g-immune/false-claim-5g-networks-suppress-the-immune-system-idUSKBN22G2OE",
        "source": "Reuters Fact Check",
        "content": (
            "Claims that 5G networks suppress the human immune system or activate COVID-19 are "
            "false. Multiple experts in immunology, virology, and electromagnetic radiation "
            "confirmed to Reuters that radio waves cannot interact with the immune system in "
            "this way. The immune system responds to biological antigens — not electromagnetic "
            "fields. Published peer-reviewed research has not identified any plausible mechanism "
            "linking 5G exposure to immune suppression or viral activation."
        ),
    },
    # ── COVID-19 death toll reporting ──────────────────────────────────────
    {
        "id": "who-covid-excess-mortality",
        "title": "WHO: COVID-19 mortality data methodology and transparency",
        "url": "https://www.who.int/data/stories/global-excess-deaths-associated-with-covid-19",
        "source": "WHO",
        "content": (
            "WHO tracks COVID-19 mortality using both confirmed death counts and excess mortality "
            "estimates. Confirmed deaths are those officially attributed to COVID-19 by national "
            "health systems. Excess mortality analysis compares observed deaths during the "
            "pandemic to expected deaths based on historical trends. Both methodologies are "
            "published openly. While undercounting may occur in countries with limited health "
            "infrastructure, claims of a systematic cover-up by governments or health "
            "organizations of death tolls 10x higher than reported are not supported by "
            "independent statistical analyses or forensic audits."
        ),
    },
    # ── Health misinformation ──────────────────────────────────────────────
    {
        "id": "who-bleach-covid",
        "title": "WHO: Drinking bleach or disinfectant does not prevent COVID-19",
        "url": "https://www.who.int/emergencies/diseases/novel-coronavirus-2019/advice-for-public/myth-busters",
        "source": "WHO",
        "content": (
            "Do NOT drink bleach, disinfectant, or any household cleaning product. These "
            "substances are poisonous and can cause serious injury or death if ingested or "
            "injected. They should never be used as treatments for COVID-19 or any disease. "
            "No disinfectant product, when swallowed or injected into the body, is safe or "
            "effective against COVID-19."
        ),
    },
    {
        "id": "cdc-ivermectin-covid",
        "title": "CDC: Ivermectin is not authorized for COVID-19 treatment",
        "url": "https://www.cdc.gov/coronavirus/2019-ncov/your-health/treatments-for-severe-illness.html",
        "source": "CDC",
        "content": (
            "Ivermectin is not authorized or approved by FDA for preventing or treating "
            "COVID-19 in humans. Clinical trials assessing ivermectin tablets for the "
            "prevention or treatment of COVID-19 have not demonstrated efficacy. People who "
            "take large doses of ivermectin can experience serious harm. FDA has received "
            "reports of patients who have been harmed after self-medicating with ivermectin "
            "intended for livestock."
        ),
    },
]


def seed(articles: list[dict] | None = None) -> int:
    """Seed articles into Chroma and Kuzu. Returns count of articles seeded."""
    articles = articles or SEED_ARTICLES
    chroma = ChromaService()
    kuzu = KuzuService()
    embedder = EmbeddingsService()

    seeded = 0
    for article in articles:
        try:
            embed = embedder.embed(article["content"][:2000])
            chroma.upsert_article(
                article["id"],
                "chunk_0",          # single-chunk article
                embed,
                article["content"],
                metadata={
                    "title": article["title"],
                    "url": article["url"],
                    "source": article.get("source", ""),
                },
            )
            kuzu.upsert_article(
                article["id"],
                article["title"],
                article.get("url", ""),
            )
            seeded += 1
            log.info("seed.article_ok", id=article["id"], title=article["title"][:60])
        except Exception as exc:
            log.error("seed.article_error", id=article["id"], error=str(exc))

    log.info("seed.complete", seeded=seeded, total=len(articles))
    return seeded


if __name__ == "__main__":
    import structlog

    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="%H:%M:%S"),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.BoundLogger,
        logger_factory=structlog.PrintLoggerFactory(),
    )
    count = seed()
    print(f"\nSeeded {count} articles into Chroma + Kuzu.")
    print("  Run main.py again -- evidence retrieval should now work.")
