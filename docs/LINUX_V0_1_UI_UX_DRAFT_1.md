# B.O.T.S. Linux v0.1 UI/UX Draft 1

Status: **PROVISIONAL / TENTATIVE FIRST DRAFT**

This document preserves the current preferred interaction and visual direction for the B.O.T.S. Linux desktop UI. It is implementation-guiding only and is deliberately expected to change after hands-on use.

This draft does **not** authorize Phase 4 or later capability work. Written interaction rules and later accepted implementation decisions outrank incidental details in visual mockups.

## Design thesis

**Maximum capability, minimum obstruction.**

The normal workspace should expose only the controls needed for frequent work. Advanced capability should remain immediately reachable, but latent until summoned through settings, drawers, inspectors, contextual actions, mode controls, or command surfaces.

The main UI should feel deceptively simple even as the underlying capability grows substantially.

## Primary shell

The first-pass shell consists of:

- a compact top bar;
- a collapsible left rail;
- the central work/chat surface;
- a minimal bottom composer;
- a fully dismissible right-side inspector/details drawer.

The shell should remain stable as later modes and capabilities are added.

## Visual direction

### Default

- sleek minimal cyberpunk;
- near-black / graphite base;
- restrained neon or electric blue accents;
- clean spacing and low visual chrome;
- capability should feel present without covering the work surface in controls.

### Blood Oath

Blood Oath should preserve the same underlying interaction architecture while switching to a substantially more aggressive visual treatment:

- black and deep red base;
- red-heavy highlights;
- more brutalist visual pressure;
- harsher borders and state treatment where appropriate;
- no requirement to rearrange the normal interaction model merely for the skin.

While Blood Oath is active, its top-bar indicator/control should act as an immediate one-click exit from Blood Oath mode.

## Left rail

The left rail is collapsible.

In its compact form it should:

- use small, closely spaced rounded-square icons;
- avoid large circular buttons;
- support small notification/unread indicators;
- consume as little horizontal space as practical while remaining easy to target.

The rail may later expose B.O.T.S. operating modes or tools such as:

- Chat;
- Work;
- Research;
- Swarm;
- Code;
- Audio;
- future modes not yet defined.

Expanded labels, a mode dropdown, command-palette switching, or other discovery mechanisms may be added later without changing the basic shell.

Mode-specific complexity should appear only when that mode or capability is summoned.

## Top bar

The top bar should remain compact and contain one model selector only.

Expected controls include:

- B.O.T.S. identity/title treatment;
- current model selector;
- compact model-tuning control adjacent to the model selector;
- current mode/state indication where useful;
- Settings as a cog inside a rounded square near the normal window controls;
- ordinary minimize/maximize/close controls;
- Blood Oath indicator / one-click exit while Blood Oath is active.

The model-tuning control should summon deeper model parameters rather than permanently occupying the workspace.

Future character-card / persona selection should be accommodated near the model/profile controls when relevant to the active mode, without forcing a shell redesign.

## Chat / work surface

The central surface should prioritize the actual work rather than configuration.

### Message layout

- assistant messages align left;
- assistant portrait/avatar sits on the outer left;
- user messages align right;
- user portrait/avatar sits on the outer right;
- message boxes size naturally to their content;
- either side may extend substantially toward the opposite side when message length requires it;
- avoid unnecessarily narrow fixed-width bubbles and avoid wasting large empty regions merely to preserve symmetry.

### Message actions

Each message should provide a compact action row at its bottom.

Initial actions:

- Copy;
- Edit;
- Branch;
- More.

Additional message-specific actions can live under More or contextual surfaces rather than being permanently visible.

## Composer

The composer should remain visually minimal.

Interaction rules:

- **Enter** sends;
- **Shift+Enter** inserts a newline.

Compact direct-action controls should include at least:

- attachment access;
- explicit manual tool invocation;
- send / stop as appropriate to current generation state.

The composer should not duplicate the model selector.

### Tool-call boundary

A tool button should provide explicit operator-directed access to currently available tools.

Automatic tool-call policy, approval rules, specialist behavior, and other infrequently changed advanced controls belong in Settings rather than occupying the main workspace.

Conceptually:

- **Settings** defines what the system may do automatically;
- **Tool button** lets the operator explicitly invoke or select capability now.

This document does not authorize implementation of the future general tool framework.

## Settings

Settings should be boring, predictable, and comprehensive.

Infrequently changed controls should live there rather than being smeared across the primary work surface. This may eventually include:

- provider/backend configuration;
- model defaults and specialist behavior;
- automatic tool-call policies;
- authority / approval policies;
- context behavior;
- appearance;
- keybindings;
- backup / restore controls;
- other specialist settings as they are actually implemented.

The existence of a future setting in this draft is not implementation authority for that capability.

## Inspector / details drawer

A right-side inspector/details surface should provide deep information without permanently consuming workspace.

It must be completely dismissible.

Expected future uses include:

- message details;
- request / generation metadata;
- provenance;
- model / backend information;
- context construction;
- tool-call details;
- failure information;
- other technical inspection surfaces.

Optional pinning may be considered later for cases where the operator wants the inspector to remain open while investigating.

Default behavior should favor temporary use and easy dismissal.

## Keyboard and low-friction behavior

The UI should bias toward low-friction operation.

In addition to the composer behavior above, later implementation should keep keyboard-first access architecturally affordable through shortcuts, configurable keybindings, and/or a command palette.

The goal is not to expose every capability at once. The goal is to make frequently used capability immediate and deeper capability one action away.

## Future expansion compatibility

The shell should be capable of absorbing later B.O.T.S. functionality without being replaced simply because the system becomes more capable.

Examples of future surfaces the shell should accommodate include:

- provider/model usability;
- character cards / personas;
- Work / Research / Swarm / Code / Audio modes;
- tool systems;
- richer inspectors;
- daemon / remote-client status;
- Android-connected workflows;
- other later B.O.T.S. capabilities.

These are compatibility goals only. They are not authorized implementation scope in this draft.

## First implementation philosophy

The first UI implementation should target this interaction skeleton rather than pixel-perfect reproduction.

The intended loop is:

1. implement the current shell and interaction rules;
2. use it in real work;
3. identify friction, wasted space, unnecessary clicks, or controls in the wrong place;
4. revise based on observed operator irritation;
5. repeat until the interface largely disappears during normal work.

Real operator experience outranks speculative UX perfection.

## Explicit non-authority / exclusions

This draft does **not** authorize:

- Phase 4 concurrency/workspace implementation;
- multiple-window or background-generation semantics beyond already accepted scope;
- a mode engine;
- a general tool framework;
- character-card implementation;
- provider/model discovery UI;
- Android implementation;
- daemon or remote-client implementation;
- Code/Git mode;
- Work/Research/Swarm/Audio mode implementation;
- attachments, search, MCP, RAG, or other later capability merely because a placeholder or affordance appears in the UI;
- backend or persistence redesign;
- any other Future Fuckery not separately approved.

Placeholders and visual affordances may exist only when they do not silently create later-phase capability.

## Mockup status

The reference mockup is illustrative only:

`docs/assets/ui-ux-draft-1-reference.svg`

It is intended to communicate approximate visual direction, density, message alignment, control placement, and the overall "simple surface / deep capability" feel.

It is **not** normative authority for exact:

- text;
- icons;
- spacing;
- dimensions;
- fonts;
- colors;
- labels;
- avatars;
- incidental controls;
- generated visual artifacts.

Where the image conflicts with this document or later accepted implementation decisions, the written/accepted authority wins.

## Revision rule

This draft is intentionally provisional.

Hands-on use is expected to change it. UI changes driven by observed friction should be recorded and adjudicated without treating deviation from this first mockup as architectural failure.
