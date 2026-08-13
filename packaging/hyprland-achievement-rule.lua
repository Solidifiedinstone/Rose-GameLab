-- Rose GameLab achievement notification.
--
-- Under Wayland an application cannot place its own window: the compositor
-- decides. So the notification asks for nothing and is pinned here instead —
-- bottom centre, undecorated, and not stealing focus from whatever is being
-- played.
--
-- Only the placement needs this. The notification's own animation and timing
-- are Qt and work everywhere; it is purely *where the window goes* that a
-- Wayland compositor reserves for itself. On X11, GameLab places it itself and
-- none of this is needed.
--
-- Copy these lines into ~/.config/hypr/custom/rules.lua (or your own rules
-- file) and run `hyprctl reload`. Delete them to remove it again.
local achievement = "^(Rose GameLab Achievement)$"

hl.window_rule({match = {title = achievement}, float = true})
hl.window_rule({match = {title = achievement},
                move = {"(monitor_w*.5-window_w*.5)", "(monitor_h-window_h-90)"}})
hl.window_rule({match = {title = achievement}, no_shadow = true})
hl.window_rule({match = {title = achievement}, no_blur = true})
hl.window_rule({match = {title = achievement}, no_initial_focus = true})
hl.window_rule({match = {title = achievement}, no_anim = true})
hl.window_rule({match = {title = achievement}, rounding = 0})
