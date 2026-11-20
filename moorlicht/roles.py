"""Rollenliste ("Skript") von Moorlicht.

Die Rollen sind reine Daten; ihre Wirkung ist in engine.py umgesetzt.
"""

from __future__ import annotations

from dataclasses import dataclass

DORF = "Dorf"
MOOR = "Moor"

DOERFLER = "Dörfler"
EIGENBROETLER = "Eigenbrötler"
SCHERGE = "Scherge"
UNHOLD = "Unhold"

TYPES = (DOERFLER, EIGENBROETLER, SCHERGE, UNHOLD)


@dataclass(frozen=True)
class Role:
    id: str
    name: str
    artikel: str  # "die" / "der" / "das"
    typ: str
    text: str

    @property
    def team(self) -> str:
        return MOOR if self.typ in (SCHERGE, UNHOLD) else DORF

    @property
    def full(self) -> str:
        return f"{self.artikel} {self.name}"

    def public(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "artikel": self.artikel,
            "typ": self.typ,
            "team": self.team,
            "text": self.text,
        }


ROLES: tuple[Role, ...] = (
    # ---------------------------------------------------------------- Dörfler
    Role(
        "seherin", "Seherin", "die", DOERFLER,
        "Jede Nacht: Wähle einen anderen Spieler. Du erfährst, ob er als „Dorf“ oder "
        "„Moor“ erscheint. Der Unhold erscheint dir stets als „Dorf“.",
    ),
    Role(
        "faehrtenleser", "Fährtenleser", "der", DOERFLER,
        "Jede Nacht: Wähle einen anderen lebenden Spieler. Du erfährst, welche Spieler er "
        "in dieser Nacht besucht hat (oder dass er niemanden besucht hat).",
    ),
    Role(
        "nachtwaechter", "Nachtwächter", "der", DOERFLER,
        "Jede Nacht: Wähle einen lebenden Spieler (auch dich selbst). Du erfährst, wie "
        "viele andere Spieler ihn in dieser Nacht besucht haben (dich nicht mitgezählt).",
    ),
    Role(
        "heilerin", "Heilerin", "die", DOERFLER,
        "Jede Nacht außer der ersten: Wähle einen anderen lebenden Spieler – nie zweimal "
        "hintereinander denselben. Er kann in dieser Nacht nicht durch den Unhold sterben.",
    ),
    Role(
        "rutengaengerin", "Rutengängerin", "die", DOERFLER,
        "Jede Nacht: Wähle einen Spieler (auch dich selbst, auch Tote). Du erfährst, wie "
        "viele der drei Plätze – der gewählte Spieler und seine beiden Sitznachbarn – als "
        "„Moor“ erscheinen.",
    ),
    Role(
        "laternentraeger", "Laternenträger", "der", DOERFLER,
        "In der ersten Nacht erfährst du, ob der Unhold von dir aus näher im Uhrzeigersinn "
        "oder näher gegen den Uhrzeigersinn sitzt. Sitzt er dir genau gegenüber, wählt der "
        "Erzähler eine Richtung.",
    ),
    Role(
        "archivarin", "Archivarin", "die", DOERFLER,
        "In der ersten Nacht erfährst du zwei Dörfler-Rollen, die in diesem Spiel nicht "
        "vergeben sind.",
    ),
    Role(
        "jaeger", "Jäger", "der", DOERFLER,
        "Wenn du stirbst (nachts oder durch Hinrichtung), wählst du sofort einen lebenden "
        "Spieler: Er stirbt ebenfalls.",
    ),
    Role(
        "schmiedin", "Schmiedin", "die", DOERFLER,
        "Der erste nächtliche Angriff des Unholds auf dich schlägt fehl: Du überlebst.",
    ),
    Role(
        "menschenkenner", "Menschenkenner", "der", DOERFLER,
        "Einmal im Spiel, nachts: Wähle einen anderen Spieler und eine Rolle. Du erfährst, "
        "ob der Spieler genau diese Rolle hat.",
    ),
    # ----------------------------------------------------------- Eigenbrötler
    Role(
        "mueller", "Müller", "der", EIGENBROETLER,
        "Du gehörst zum Dorf, erscheinst für Seherin und Rutengängerin aber als „Moor“.",
    ),
    Role(
        "schlafwandler", "Schlafwandler", "der", EIGENBROETLER,
        "Jede Nacht besuchst du, ohne es zu merken, einen zufälligen anderen lebenden "
        "Spieler.",
    ),
    Role(
        "pechvogel", "Pechvogel", "der", EIGENBROETLER,
        "Wirst du hingerichtet, darf der Unhold in der folgenden Nacht zwei Spieler töten.",
    ),
    # ---------------------------------------------------------------- Schergen
    Role(
        "nebelkraehe", "Nebelkrähe", "die", SCHERGE,
        "Jede Nacht: Wähle einen anderen lebenden Spieler – nie zweimal hintereinander "
        "denselben. Er steht bis zur nächsten Abenddämmerung im Nebel.",
    ),
    Role(
        "verleumder", "Verleumder", "der", SCHERGE,
        "Jede Nacht: Wähle einen anderen Spieler. Er erscheint in dieser Nacht für Seherin "
        "und Rutengängerin als „Moor“.",
    ),
    Role(
        "erpresser", "Erpresser", "der", SCHERGE,
        "Jede Nacht: Wähle einen anderen lebenden Spieler. Er darf am folgenden Tag weder "
        "nominieren noch abstimmen und erfährt das am Morgen.",
    ),
    # ----------------------------------------------------------------- Unholde
    Role(
        "irrlicht", "Irrlicht", "das", UNHOLD,
        "Jede Nacht außer der ersten: Wähle einen anderen lebenden Spieler. Er stirbt.",
    ),
    Role(
        "nachtmahr", "Nachtmahr", "der", UNHOLD,
        "In der ersten Nacht: Wähle einen anderen Spieler. Er steht für den Rest des "
        "Spiels im Nebel. Jede Nacht außer der ersten: Wähle einen anderen lebenden "
        "Spieler. Er stirbt.",
    ),
)

ROLE_BY_ID: dict[str, Role] = {r.id: r for r in ROLES}
ROLE_BY_NAME: dict[str, Role] = {r.name: r for r in ROLES}


def roles_of(typ: str) -> list[Role]:
    return [r for r in ROLES if r.typ == typ]


# Spielerzahl -> (Dörfler, Eigenbrötler, Schergen, Unholde)
DISTRIBUTION: dict[int, tuple[int, int, int, int]] = {
    7: (5, 0, 1, 1),
    8: (5, 1, 1, 1),
    9: (5, 2, 1, 1),
}

PLAYER_NAMES = ("Anna", "Bruno", "Clara", "David", "Emma", "Felix", "Greta", "Hugo", "Ida")
