"""ECOReward kiosk building blocks: settings, the sorter hardware link, the transaction flow and the UI.

app.py wires them to the camera, the model and Firestore. Nothing in this package touches Firestore,
the camera or the model directly, so every part can be tested without hardware.
"""
