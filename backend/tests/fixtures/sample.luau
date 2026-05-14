-- Code lane test fixture — Luau module for Roblox combat (the user's example domain).
local TweenService = game:GetService("TweenService")
local ReplicatedStorage = game:GetService("ReplicatedStorage")
local CombatConfig = require(ReplicatedStorage.Config.CombatConfig)

local Combat = {}

function Combat.PunchAttack(player, config)
	config = config or CombatConfig
	local character = player.Character
	if not character then
		return false
	end
	local hitEffect = Instance.new("Part")
	hitEffect.Color = config.PunchColor or Color3.fromRGB(255, 0, 0)
	hitEffect.Parent = workspace.Effects
	return true
end

function Combat.Hitbox(part, callback)
	local touched = part.Touched:Connect(function(other)
		if other.Parent:FindFirstChild("Humanoid") then
			callback(other)
		end
	end)
	return touched
end

return Combat
