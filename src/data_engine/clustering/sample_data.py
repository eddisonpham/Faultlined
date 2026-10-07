"""Task strings to propose over when the catalog has nothing to offer."""

from __future__ import annotations

SAMPLE_TASKS: dict[str, int] = {
    "Push the T-shaped block onto the T-shaped target.": 120,
    "Insert the peg into the socket.": 84,
    "Pick up the cube with the right arm and transfer it to the left arm.": 96,
    "Pick up the cube and lift it.": 140,
    "Push the cube onto the target.": 110,
    "put the white mug on the left plate and put the yellow and white mug on the right plate": 12,
    "put the white mug on the plate and put the chocolate pudding to the right of the plate": 9,
    "put the yellow and white mug in the microwave and close it": 7,
    "turn on the stove and put the moka pot on it": 15,
    "put both the alphabet soup and the cream cheese box in the basket": 6,
    "put both the alphabet soup and the tomato sauce in the basket": 6,
    "put both moka pots on the stove": 11,
    "put both the cream cheese box and the butter in the basket": 5,
    "put the black bowl in the bottom drawer of the cabinet and close it": 8,
    "pick up the book and place it in the back compartment of the caddy": 4,
    "put the bowl on the plate": 22,
    "put the wine bottle on the rack": 5,
    "open the top drawer and put the bowl inside": 9,
    "put the cream cheese in the bowl": 7,
    "put the wine bottle on top of the cabinet": 4,
    "push the plate to the front of the stove": 3,
    "turn on the stove": 19,
    "put the bowl on the stove": 6,
    "put the bowl on top of the cabinet": 5,
    "open the middle drawer of the cabinet": 13,
    "pick up the orange juice and place it in the basket": 8,
    "pick up the ketchup and place it in the basket": 7,
    "pick up the cream cheese and place it in the basket": 7,
    "pick up the bbq sauce and place it in the basket": 6,
    "pick up the alphabet soup and place it in the basket": 6,
    "pick up the milk and place it in the basket": 9,
    "pick up the salad dressing and place it in the basket": 6,
    "pick up the butter and place it in the basket": 8,
    "pick up the tomato sauce and place it in the basket": 7,
    "pick up the chocolate pudding and place it in the basket": 7,
    "pick up the black bowl next to the cookie box and place it on the plate": 5,
    "pick up the black bowl in the top drawer of the wooden cabinet and place it on the plate": 5,
    "pick up the black bowl on the ramekin and place it on the plate": 5,
    "pick up the black bowl on the stove and place it on the plate": 5,
    "pick up the black bowl between the plate and the ramekin and place it on the plate": 4,
    "pick up the black bowl on the cookie box and place it on the plate": 4,
    "pick up the black bowl next to the plate and place it on the plate": 5,
    "pick up the black bowl next to the ramekin and place it on the plate": 4,
    "pick up the black bowl from table center and place it on the plate": 4,
    "pick up the black bowl on the wooden cabinet and place it on the plate": 4,
    "pink lego brick into the transparent box": 50,
}

SAMPLE_NOTE = "illustrative LeRobot task sentences, not catalog data"


def sample_tasks() -> dict[str, int]:
    """A copy, so a caller cannot mutate the shipped counts."""
    return dict(SAMPLE_TASKS)
