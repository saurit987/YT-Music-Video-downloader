# YouTube Music Downloader - Design Specification

## Overview
A professional, modern Material Design 3 interface for a music downloading utility, featuring a high-contrast Red and Black theme with glassmorphism elements.

## Design Tokens

### Colors
- **Primary (Accent):** #FF0000 (Pure Red) - Used for primary CTAs, active states, and highlights.
- **Surface (Main Background):** #000000 (Black).
- **Surface (Cards/Containers):** rgba(30, 30, 30, 0.7) with `backdrop-filter: blur(12px)`.
- **Text (Primary):** #FFFFFF (White).
- **Text (Secondary):** rgba(255, 255, 255, 0.7).
- **Border:** rgba(255, 255, 255, 0.1).

### Typography
- **Font Family:** 'JetBrains Mono', monospace.
- **Weights:**
  - Headings: SemiBold (600)
  - Body/Labels: Regular (400)
  - CTA/Buttons: SemiBold (600)

### Components
- **Inputs:** Dark background, red focus border, mono font.
- **Buttons:** Solid Red (#FF0000) for primary; Outlined for secondary.
- **Cards:** Translucent black with background blur, 16px border-radius.

## Implementation Notes
- Use Tailwind CSS for rapid styling.
- Apply `backdrop-filter: blur()` for the glassmorphism effect on the main downloader card.
- Ensure high contrast for accessibility between red accents and dark backgrounds.
