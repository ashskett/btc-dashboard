class EngineState:

    def __init__(self):

        # market data
        self.price = None
        self.atr = None
        self.volatility_ratio = None

        # regime
        self.regime = None
        self.session = None

        # grid
        self.grid_width = None
        self.center = None
        self.grid_low = None
        self.grid_high = None
        self.levels = None
        self.step = None

        # inventory
        self.btc_ratio = None
        self.skew = None
        self.inventory_mode = "NORMAL"

        # safety
        self.compression = False

        # events (logged each cycle)
        self.drift_triggered = False

        # trend strength
        self.trending_up   = False
        self.trending_down = False
        self.gap_ratio     = 0.0

        # tiers (populated by calculate_grid_parameters)
        self.tiers = []