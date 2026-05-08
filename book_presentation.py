"""
book_presentation.py — Catálogo de mídia da Chef Sandra (livros + provas sociais).

Markers que a IA emite no prompt → o watcher intercepta e dispara mídia:
  - LIBROS_MARKER         → 5 imagens dos livros (PASO 4)
  - PRUEBA_DEFAULT_MARKER → 1 prova social fixa (PASO 4.5, entre livros e preço)
  - PRUEBA_OBJECION_MARKER → 1 prova social reativa (objeção de eficácia/hesitação)

Cada marker está mapeado em MEDIA_DISPATCH para uma lista de itens
{image, caption}. O watcher separa o texto antes/depois do marcador e
intercala como mensagens normais.
"""

from pathlib import Path

CONTENT_DIR              = Path.home() / "chef-sandra" / "conteudo"
LIBROS_MARKER            = "[[ENVIAR_LIBROS]]"
PRUEBA_DEFAULT_MARKER    = "[[ENVIAR_PRUEBA_DEFAULT]]"
PRUEBA_OBJECION_MARKER   = "[[ENVIAR_PRUEBA_OBJECION]]"

BOOK_PRESENTATIONS = [
    {
        "image": CONTENT_DIR / "livro1.png",
        "caption": (
            "📗 *Libro 1 – Panes para Diabéticos*\n"
            "10 recetas de panes con bajo índice glucémico. Pan de zanahoria, "
            "almendras, coco, avena y más — para comer pan sin miedo a la glucosa."
        ),
    },
    {
        "image": CONTENT_DIR / "livro2.png",
        "caption": (
            "📘 *Libro 2 – Panes Trenzados Sin Gluten*\n"
            "15 recetas de panes trenzados artesanales, sin gluten, con texturas "
            "increíbles. Quinoa, chía, linaza, proteico... para los que aman el pan de verdad."
        ),
    },
    {
        "image": CONTENT_DIR / "livro3.png",
        "caption": (
            "📙 *Libro 3 – Postres Sin Azúcar*\n"
            "18 recetas de postres deliciosos sin azúcar refinada. Petit gateau fit, "
            "brownie de banana, helado proteico, brigadeiro fit, galletas y más."
        ),
    },
    {
        "image": CONTENT_DIR / "livro4.png",
        "caption": (
            "📕 *Libro 4 – Tortas Sin Culpa*\n"
            "20 recetas de tortas equilibradas que puedes comer con placer. "
            "De chocolate 70%, manzana, limón, naranja, coco, zanahoria y otras."
        ),
    },
    {
        "image": CONTENT_DIR / "livro5.png",
        "caption": (
            "📒 *Libro 5 – Almuerzo y Cena*\n"
            "10 recetas de comidas saladas seguras para diabéticos. "
            "Platos completos y nutritivos para el día a día."
        ),
    },
]

# Provas sociais: imagens são prints de conversa real, então caption fica vazio
# (o texto de enquadramento vem antes do marcador, no próprio prompt da Chef).
SOCIAL_PROOF_DEFAULT = {
    "image":   CONTENT_DIR / "proba1.png",   # Mariana — brownie de banana, "sin culpa"
    "caption": "",
}
SOCIAL_PROOF_OBJECION = {
    "image":   CONTENT_DIR / "proba2.png",   # Roberto — pan de avena, glucosa controlada
    "caption": "",
}

MEDIA_DISPATCH = {
    LIBROS_MARKER:          BOOK_PRESENTATIONS,
    PRUEBA_DEFAULT_MARKER:  [SOCIAL_PROOF_DEFAULT],
    PRUEBA_OBJECION_MARKER: [SOCIAL_PROOF_OBJECION],
}
