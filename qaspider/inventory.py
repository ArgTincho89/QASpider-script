"""Boolean-only DOM inventory categories and their browser-side extraction."""

from __future__ import annotations

CANONICAL_INVENTORY_KEYS = (
    "links",
    "internalLinks",
    "externalLinks",
    "navigation",
    "images",
    "picture",
    "svg",
    "canvas",
    "headings",
    "forms",
    "inputs",
    "textareas",
    "selects",
    "buttons",
    "labels",
    "fieldsets",
    "tables",
    "tableHeaders",
    "tableCaption",
    "lists",
    "unorderedLists",
    "orderedLists",
    "descriptionLists",
    "video",
    "audio",
    "track",
    "iframes",
    "objects",
    "embeds",
    "embeddedContent",
    "landmarks",
    "header",
    "nav",
    "main",
    "footer",
    "aside",
    "section",
    "article",
    "aria",
    "roles",
    "interactiveElements",
    "tabindex",
    "dialogs",
    "details",
    "summary",
    "progress",
    "meter",
    "paragraphs",
    "time",
    "abbr",
    "mark",
    "languageAttributes",
    "contentEditable",
    "customElements",
    "shadowDom",
)

INVENTORY_SELECTORS = {
    "links": "a[href], area[href], [role='link'][href]",
    "internalLinks": None,
    "externalLinks": None,
    "navigation": "nav, [role='navigation']",
    "media": "img, picture, video, audio, source, track, iframe, object, embed",
    "images": "img",
    "picture": "picture",
    "svg": "svg",
    "canvas": "canvas",
    "video": "video",
    "audio": "audio",
    "mediaSources": "source",
    "track": "track",
    "mediaTracks": "track",
    "iframes": "iframe",
    "objects": "object",
    "embeds": "embed",
    "embeddedContent": "iframe, object, embed",
    "headings": "h1, h2, h3, h4, h5, h6, [role='heading']",
    "h1": "h1",
    "h2": "h2",
    "h3": "h3",
    "h4": "h4",
    "h5": "h5",
    "h6": "h6",
    "forms": "form",
    "formControls": "input, select, textarea, button, output",
    "inputs": "input",
    "textareas": "textarea",
    "selects": "select",
    "buttons": "button",
    "labels": "label",
    "fieldsets": "fieldset",
    "tables": "table",
    "tableHeaders": "th",
    "tableCaption": "caption",
    "lists": "ul, ol, dl, [role='list']",
    "orderedLists": "ol",
    "unorderedLists": "ul",
    "descriptionLists": "dl",
    "listItems": "li, dt, dd",
    "landmarks": "header, nav, main, footer, aside, section, article, [role='banner'], [role='complementary'], [role='contentinfo'], [role='form'], [role='main'], [role='navigation'], [role='region'], [role='search']",
    "header": "header, [role='banner']",
    "nav": "nav, [role='navigation']",
    "main": "main, [role='main']",
    "footer": "footer, [role='contentinfo']",
    "aside": "aside, [role='complementary']",
    "section": "section, [role='region']",
    "article": "article, [role='article']",
    "aria": None,
    "roles": "[role]",
    "interactiveElements": "a[href], button, input:not([type='hidden']), select, textarea, summary, [tabindex], [contenteditable='true'], [role='button'], [role='link']",
    "tabindex": "[tabindex]",
    "dialogs": "dialog, [role='dialog'], [role='alertdialog']",
    "details": "details",
    "summary": "summary",
    "progress": "progress",
    "meter": "meter",
    "paragraphs": "p",
    "time": "time",
    "abbr": "abbr",
    "mark": "mark",
    "languageAttributes": "[lang]",
    "contentEditable": "[contenteditable]:not([contenteditable='false'])",
    "customElements": None,
    "shadowDom": None,
}

INVENTORY_KEYS = tuple(INVENTORY_SELECTORS)


def empty_inventory() -> dict[str, bool]:
    """Create the complete inventory shape with every category set to false."""
    return dict.fromkeys(INVENTORY_KEYS, False)


def inspect_page(page, origin: str) -> tuple[dict[str, bool], list[str]]:
    """Inspect rendered DOM, returning boolean categories and raw anchor URLs."""
    result = page.evaluate(
        """({ selectors, origin }) => {
          const inventory = {};
          for (const [key, selector] of Object.entries(selectors)) {
            if (key === 'internalLinks' || key === 'externalLinks') {
                const anchors = [...document.querySelectorAll('a[href], area[href], [role="link"][href]')];
              inventory[key] = anchors.some((anchor) => {
                try {
                  const target = new URL(anchor.getAttribute('href'), document.baseURI);
                  return ['http:', 'https:'].includes(target.protocol)
                    && (key === 'internalLinks' ? target.origin === origin : target.origin !== origin);
                } catch { return false; }
              });
            } else if (key === 'aria') {
              inventory[key] = [...document.querySelectorAll('*')].some((element) =>
                [...element.attributes].some((attribute) => attribute.name.toLowerCase().startsWith('aria-')));
            } else if (key === 'customElements') {
              inventory[key] = [...document.querySelectorAll('*')].some((element) => element.localName.includes('-'));
            } else if (key === 'shadowDom') {
              inventory[key] = [...document.querySelectorAll('*')].some((element) => Boolean(element.shadowRoot));
            } else {
              inventory[key] = Boolean(selector && document.querySelector(selector));
            }
          }
          return {
            inventory,
            anchors: [...document.querySelectorAll('a[href], area[href], [role="link"][href]')]
              .map((link) => {
                try { return new URL(link.getAttribute('href'), document.baseURI).href; }
                catch { return null; }
              })
              .filter(Boolean),
          };
        }""",
        {"selectors": INVENTORY_SELECTORS, "origin": origin},
    )
    inventory = empty_inventory()
    for key in INVENTORY_KEYS:
        inventory[key] = bool(result["inventory"].get(key, False))
    return inventory, result["anchors"]


def extract_navigable_links(page) -> list[str]:
    """Extract passive navigable URLs without inspecting page element categories."""
    return page.evaluate(
        """() => [...document.querySelectorAll('a[href], area[href], [role="link"][href]')]
          .map((link) => {
            try { return new URL(link.getAttribute('href'), document.baseURI).href; }
            catch { return null; }
          })
          .filter(Boolean)"""
    )
